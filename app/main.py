"""librarian — media deletion portal.

Phase 1: music library browse/plan/execute, slskd leftover sweep, audit log.
See SPEC.md for the behaviour contract and ROADMAP.md for what is not here yet.
"""

from __future__ import annotations

import base64
import dataclasses
import hmac
import logging
import os
import secrets
import time
from html import escape
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from app import audit
from app.adapters.base import DeletePlan, StalePlanError
from app.adapters.books import BooksAdapter
from app.adapters.downloads import DownloadsAdapter
from app.adapters.music import MusicAdapter
from app.adapters.video import VideoAdapter
from app.arr import ArrClient, ArrError
from app.lidarr import LidarrClient, LidarrError
from app.scan import PathJailError
from app.slskd import SlskdClient

log = logging.getLogger(__name__)

app = FastAPI(title="librarian", docs_url=None, redoc_url=None, openapi_url=None)
_basic = HTTPBasic(auto_error=False)

SESSION_COOKIE = "librarian_session"
SESSION_TTL = 30 * 24 * 3600  # 30 days


class _RedirectToLogin(Exception):
    """Raised by require_auth when the caller is a browser navigating to a page.

    A dependency cannot return a response, so this is raised and turned into a 303 by
    the handler below.
    """

    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


def _wants_html(request: Request) -> bool:
    """True for a browser following a link, false for fetch/curl/the test client.

    Browsers send `Accept: text/html,...` on navigation and `Accept: */*` on `fetch()`,
    which is exactly the distinction needed: navigations get the login page, API calls
    get a 401 the frontend can act on.
    """
    return "text/html" in request.headers.get("accept", "")


def _safe_next(candidate: str | None) -> str:
    """Only ever redirect to a path on this host.

    `//evil.com` and `https://evil.com` are both absolute references a browser would
    follow off-site, so anything that is not a single-slash-prefixed path is discarded.
    """
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate


@app.exception_handler(_RedirectToLogin)
def _login_redirect_handler(request: Request, exc: _RedirectToLogin) -> Response:
    nxt = _safe_next(exc.next_path)
    suffix = f"?next={quote(nxt, safe='/')}" if nxt != "/" else ""
    return RedirectResponse(f"/login{suffix}", status_code=status.HTTP_303_SEE_OTHER)


class PlanRequest(BaseModel):
    # Capped so a single request cannot ask the server to walk the whole library many
    # times over; the UI selects albums, not thousands of paths.
    paths: list[str] = Field(min_length=1, max_length=200)


class ExecuteRequest(PlanRequest):
    digest: str
    confirm: str


def _audit_path() -> Path:
    return Path(os.environ.get("LIBRARIAN_CONFIG", "/config")) / "audit.log"


def _record_intent(plan: DeletePlan, *, actor: str, confirmed: str) -> None:
    """Store intent durably or stop before any destructive operation."""
    try:
        audit.record_intent(_audit_path(), plan, actor=actor, confirmed=confirmed)
    except audit.AuditWriteError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc


def _expected_credentials() -> tuple[str, str]:
    """Read the configured credentials, failing closed if they are missing.

    An unconfigured deployment must be broken and obvious rather than silently
    writable to anything that can reach the service.
    """
    user = os.environ.get("LIBRARIAN_BASIC_USER", "")
    password = os.environ.get("LIBRARIAN_BASIC_PASS", "")
    if not user or not password:
        raise RuntimeError("LIBRARIAN_BASIC_USER and LIBRARIAN_BASIC_PASS must be set")
    return user, password


def _cookie_secure() -> bool:
    """Return whether browser session cookies require HTTPS.

    Secure cookies are the safe default. A private HTTP deployment must opt out
    explicitly rather than inheriting an unsafe public default.
    """
    value = os.environ.get("LIBRARIAN_COOKIE_SECURE", "true").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError("LIBRARIAN_COOKIE_SECURE must be true or false")


def _credentials_ok(supplied_user: str, supplied_password: str) -> bool:
    """Constant-time credential check, shared by Basic auth and the login form.

    Everything is encoded to UTF-8 bytes first. `secrets.compare_digest` raises
    TypeError on a `str` holding any non-ASCII character, and the login form feeds it
    raw user input — so `POST /login` with `username=ü` returned 500 instead of 401,
    and a non-ASCII character in the configured password made login impossible. Basic
    auth never reached this path: that header decodes as ASCII and fails earlier.
    """
    user, password = _expected_credentials()
    # Both comparisons always run: short-circuiting on the username would leak whether
    # a username exists via response timing.
    user_ok = secrets.compare_digest(supplied_user.encode("utf-8"), user.encode("utf-8"))
    password_ok = secrets.compare_digest(
        supplied_password.encode("utf-8"), password.encode("utf-8")
    )
    return user_ok and password_ok


def _session_secret() -> bytes:
    """Derive the cookie-signing key from the configured credentials.

    No new environment variable, and a deliberate property: rotating the password
    invalidates every outstanding session, which is what you want from a rotation.
    """
    user, password = _expected_credentials()
    return hmac.new(b"librarian-session-v1", f"{user}:{password}".encode(), "sha256").digest()


def _epoch_path() -> Path:
    """File holding the revocation epoch. Lives beside the audit log in /config."""
    return Path(os.environ.get("LIBRARIAN_CONFIG", "/config")) / "session-epoch"


def _now_us() -> int:
    """Microseconds since the epoch, as an int.

    Timestamps in the token are microseconds rather than seconds because the token is
    dot-delimited, so a float's decimal point cannot go in it — and second resolution
    is too coarse: logging straight back in after signing out happens within the same
    second, and the fresh token was being caught by its own logout.
    """
    return int(time.time() * 1_000_000)


def _revoked_before() -> int:
    """Sessions issued at or before this microsecond timestamp are dead.

    On disk rather than in memory so that signing out survives a container restart —
    an in-process variable would quietly un-revoke every token on the next rebuild,
    and this container is rebuilt often.
    """
    try:
        return int(_epoch_path().read_text().strip())
    except (OSError, ValueError):
        return 0


def _revoke_all_sessions() -> None:
    """Sign out. This is estate-wide, not per-token: librarian is single-user, so
    there is no second session that should survive, and a stateless token cannot be
    individually revoked without exactly the server-side store this design avoids."""
    path = _epoch_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(_now_us()))
    except OSError:
        # A read-only /config must not make logout *appear* to work.
        log.exception("could not write the session epoch; sessions were NOT revoked")
        raise


def _issue_session(user: str, ttl: int = SESSION_TTL) -> str:
    """`<user>.<issued>.<expiry>.<hmac>`, all base64url.

    Stateless apart from the revocation epoch: no per-session store to lose on
    restart. `issued` is carried explicitly rather than derived from `expiry - TTL`
    so that a token minted with a non-default ttl still reports its real issue time.
    """
    now = _now_us()
    payload = f"{base64.urlsafe_b64encode(user.encode()).decode()}.{now}.{now + ttl * 1_000_000}"
    sig = hmac.new(_session_secret(), payload.encode(), "sha256").digest()
    return f"{payload}.{base64.urlsafe_b64encode(sig).decode()}"


def _verify_session(token: str) -> str | None:
    """Return the username a valid, unexpired, unrevoked token names, else None."""
    try:
        user_b64, issued, expiry, sig_b64 = token.split(".")
        payload = f"{user_b64}.{issued}.{expiry}"
        expected = hmac.new(_session_secret(), payload.encode(), "sha256").digest()
        # compare_digest before looking at the timestamps: a forged token must not be
        # distinguishable from an expired one.
        if not hmac.compare_digest(base64.urlsafe_b64decode(sig_b64), expected):
            return None
        if int(expiry) < _now_us():
            return None
        if int(issued) <= _revoked_before():
            return None
        return base64.urlsafe_b64decode(user_b64).decode()
    except (ValueError, TypeError, UnicodeDecodeError):
        # Any malformed cookie is simply not a session. Never a 500.
        return None


def require_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_basic),
) -> str:
    """Auth dependency for every route except /healthz and the login page.

    Accepts either a signed session cookie (what a browser gets after the themed login
    page) or HTTP Basic (what curl, the deploy gate and the screenshot harness use).
    Basic is kept deliberately: dropping it would break the health checks in AGENTS.md
    and give no security benefit, since it is the same credential either way.
    """
    # Called unconditionally and first: it raises when the service is unconfigured, and
    # that must surface as a 500 no matter what the caller sent. Reaching the 401 below
    # instead would make a broken deployment look like a wrong password.
    _expected_credentials()

    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        session_user = _verify_session(cookie)
        if session_user is not None:
            return session_user

    if credentials is not None and _credentials_ok(credentials.username, credentials.password):
        return credentials.username

    # A browser navigating to a page gets the themed login page; anything else gets a
    # plain 401. The `Basic` challenge is only sent to non-browsers — sending it to a
    # browser is what summons the unstylable native popup this phase exists to remove.
    if _wants_html(request):
        # Path AND query: dropping the query made `next` mostly decorative, since a
        # deep link's state would not survive the round trip through the login page.
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        raise _RedirectToLogin(target)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="unauthorized",
        headers={"WWW-Authenticate": "Basic"},
    )


def get_music_adapter() -> MusicAdapter:
    """Build the music adapter from the environment.

    Constructed per request rather than at import time so a misconfigured Lidarr URL
    surfaces as a 502 on the tree call, not a container that will not start.
    """
    api_key = os.environ.get("LIDARR_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="LIDARR_API_KEY is not set")
    client = LidarrClient(os.environ.get("LIDARR_URL", "http://lidarr:8686"), api_key)
    return MusicAdapter(
        root=os.environ.get("MUSIC_ROOT", "/music"),
        client=client,
        lidarr_root=os.environ.get("LIDARR_MUSIC_ROOT", "/music"),
    )


def get_downloads_adapter() -> DownloadsAdapter:
    """The slskd download tree. The slskd client is optional — without an API key the
    directories are still listable and deletable, only the transfer records survive."""
    api_key = os.environ.get("SLSKD_API_KEY", "")
    client = (
        SlskdClient(os.environ.get("SLSKD_URL", "http://slskd:5030"), api_key) if api_key else None
    )
    return DownloadsAdapter(
        root=os.environ.get("SLSKD_DOWNLOADS_ROOT", "/slskd-downloads"), client=client
    )


def get_video_adapter() -> VideoAdapter:
    """Radarr + Sonarr over the shared /data media root.

    Either manager may be absent; the adapter degrades to whichever keys are configured,
    and files the missing one owns simply read as orphans.
    """
    clients: dict[str, ArrClient] = {}
    for flavour, key_env, url_env, default in (
        ("radarr", "RADARR_API_KEY", "RADARR_URL", "http://radarr:7878"),
        ("sonarr", "SONARR_API_KEY", "SONARR_URL", "http://sonarr:8989"),
    ):
        key = os.environ.get(key_env, "")
        if key:
            clients[flavour] = ArrClient(os.environ.get(url_env, default), key, flavour)
    if not clients:
        raise HTTPException(status_code=500, detail="no RADARR_API_KEY or SONARR_API_KEY set")
    return VideoAdapter(
        root=os.environ.get("VIDEO_ROOT", "/data"),
        clients=clients,
        arr_root=os.environ.get("ARR_MEDIA_ROOT", "/data"),
    )


def get_books_adapter() -> BooksAdapter:
    """Kavita's library. No client — there is no *arr to unmonitor and no API key here."""
    return BooksAdapter(root=os.environ.get("BOOKS_ROOT", "/books"))


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    """Unauthenticated liveness probe. Reveals nothing about the library."""
    return "ok"


@app.get("/librarian.css", response_class=FileResponse)
def stylesheet() -> FileResponse:
    """Serve the bundled stylesheet needed by the unauthenticated login page."""
    path = Path(__file__).parent / "static" / "librarian.css"
    return FileResponse(path, media_type="text/css")


@app.get("/api/music/tree")
def music_tree(
    path: str = Query("", description="path relative to the library root; empty is the root"),
    _user: str = Depends(require_auth),
) -> dict:
    """One level of the music library, each entry tagged managed or orphan."""
    adapter = get_music_adapter()
    try:
        try:
            index = adapter.index()
        except LidarrError as exc:
            raise HTTPException(status_code=502, detail=f"lidarr unreachable: {exc}") from exc
        try:
            entries = adapter.tree(path, index=index)
        except PathJailError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        shared = adapter.shared_directories(index=index) if not path else {}
    finally:
        adapter.close()

    return {
        "path": path,
        "entries": [dataclasses.asdict(entry) for entry in entries],
        "shared_directories": shared,
    }


@app.post("/api/music/plan")
def music_plan(body: PlanRequest, _user: str = Depends(require_auth)) -> dict:
    """Preview a deletion. Writes nothing and calls no destructive API."""
    adapter = get_music_adapter()
    try:
        try:
            index = adapter.index()
        except LidarrError as exc:
            raise HTTPException(status_code=502, detail=f"lidarr unreachable: {exc}") from exc
        try:
            plan = adapter.plan(body.paths, index=index)
        except PathJailError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        adapter.close()
    return dataclasses.asdict(plan)


@app.post("/api/music/execute")
def music_execute(body: ExecuteRequest, user: str = Depends(require_auth)) -> dict:
    """Run a plan. Irreversible.

    Three gates before anything is touched: the paths go back through the jail, the
    typed confirmation must match the plan's phrase, and the digest must still match the
    tree. Only then does the adapter act.
    """
    adapter = get_music_adapter()
    try:
        try:
            index = adapter.index()
        except LidarrError as exc:
            raise HTTPException(status_code=502, detail=f"lidarr unreachable: {exc}") from exc
        try:
            proposed = adapter.plan(body.paths, index=index)
        except PathJailError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if body.confirm != proposed.confirm_phrase:
            raise HTTPException(
                status_code=400,
                detail=f"confirmation must be exactly {proposed.confirm_phrase!r}",
            )
        if body.digest != proposed.digest:
            raise HTTPException(
                status_code=409,
                detail="the library changed since this plan was built; re-plan first",
            )

        # Written BEFORE the first destructive call. If the process dies mid-execute the
        # files are gone either way; an intent line with no matching outcome is what tells
        # a later investigation that librarian was interrupted rather than never involved.
        _record_intent(proposed, actor=user, confirmed=body.confirm)
        try:
            result = adapter.execute(proposed, index=index)
        except StalePlanError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        adapter.close()

    audit.record(_audit_path(), result, actor=user, confirmed=body.confirm)
    return dataclasses.asdict(result)


@app.get("/api/video/tree")
def video_tree(
    path: str = Query("", description="path relative to the media root; empty is the root"),
    _user: str = Depends(require_auth),
) -> dict:
    adapter = get_video_adapter()
    try:
        try:
            index = adapter.index()
        except ArrError as exc:
            raise HTTPException(status_code=502, detail=f"arr unreachable: {exc}") from exc
        try:
            entries = adapter.tree(path, index=index)
        except PathJailError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        adapter.close()
    return {"path": path, "entries": [dataclasses.asdict(e) for e in entries]}


@app.post("/api/video/plan")
def video_plan(body: PlanRequest, _user: str = Depends(require_auth)) -> dict:
    adapter = get_video_adapter()
    try:
        try:
            plan = adapter.plan(body.paths, index=adapter.index())
        except ArrError as exc:
            raise HTTPException(status_code=502, detail=f"arr unreachable: {exc}") from exc
        except (PathJailError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        adapter.close()
    return dataclasses.asdict(plan)


@app.post("/api/video/execute")
def video_execute(body: ExecuteRequest, user: str = Depends(require_auth)) -> dict:
    adapter = get_video_adapter()
    try:
        index = adapter.index()
        try:
            proposed = adapter.plan(body.paths, index=index)
        except (PathJailError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if body.confirm != proposed.confirm_phrase:
            raise HTTPException(
                status_code=400,
                detail=f"confirmation must be exactly {proposed.confirm_phrase!r}",
            )
        if body.digest != proposed.digest:
            raise HTTPException(
                status_code=409,
                detail="the library changed since this plan was built; re-plan first",
            )
        _record_intent(proposed, actor=user, confirmed=body.confirm)
        try:
            result = adapter.execute(proposed, index=index)
        except StalePlanError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        adapter.close()
    audit.record(_audit_path(), result, actor=user, confirmed=body.confirm)
    return dataclasses.asdict(result)


@app.get("/api/books/tree")
def books_tree(
    path: str = Query(""),
    _user: str = Depends(require_auth),
) -> dict:
    adapter = get_books_adapter()
    try:
        entries = adapter.tree(path)
    except PathJailError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"path": path, "entries": [dataclasses.asdict(e) for e in entries]}


@app.post("/api/books/plan")
def books_plan(body: PlanRequest, _user: str = Depends(require_auth)) -> dict:
    try:
        return dataclasses.asdict(get_books_adapter().plan(body.paths))
    except (PathJailError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/books/execute")
def books_execute(body: ExecuteRequest, user: str = Depends(require_auth)) -> dict:
    adapter = get_books_adapter()
    try:
        proposed = adapter.plan(body.paths)
    except (PathJailError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.confirm != proposed.confirm_phrase:
        raise HTTPException(
            status_code=400, detail=f"confirmation must be exactly {proposed.confirm_phrase!r}"
        )
    if body.digest != proposed.digest:
        raise HTTPException(
            status_code=409, detail="the library changed since this plan was built; re-plan first"
        )
    _record_intent(proposed, actor=user, confirmed=body.confirm)
    try:
        result = adapter.execute(proposed)
    except StalePlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit.record(_audit_path(), result, actor=user, confirmed=body.confirm)
    return dataclasses.asdict(result)


@app.get("/api/slskd/leftovers")
def slskd_leftovers(
    min_age_days: float = Query(0.0, ge=0),
    _user: str = Depends(require_auth),
) -> dict:
    """Directories in the slskd download tree, with their age, size and slskd records."""
    adapter = get_downloads_adapter()
    try:
        items = adapter.leftovers(min_age_days=min_age_days)
    finally:
        adapter.close()
    return {
        "entries": [dataclasses.asdict(item) for item in items],
        "total_bytes": sum(i.size for i in items if not i.protected),
    }


@app.post("/api/slskd/plan")
def slskd_plan(body: PlanRequest, _user: str = Depends(require_auth)) -> dict:
    """Preview directory deletion and matching slskd record cleanup."""
    adapter = get_downloads_adapter()
    try:
        plan = adapter.plan(body.paths)
    except (PathJailError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        adapter.close()
    return dataclasses.asdict(plan)


@app.post("/api/slskd/execute")
def slskd_execute(body: ExecuteRequest, user: str = Depends(require_auth)) -> dict:
    """Execute a current, confirmed slskd cleanup plan."""
    adapter = get_downloads_adapter()
    try:
        try:
            proposed = adapter.plan(body.paths)
        except (PathJailError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if body.confirm != proposed.confirm_phrase:
            raise HTTPException(
                status_code=400,
                detail=f"confirmation must be exactly {proposed.confirm_phrase!r}",
            )
        if body.digest != proposed.digest:
            raise HTTPException(
                status_code=409,
                detail="the download tree changed since this plan was built; re-plan first",
            )
        _record_intent(proposed, actor=user, confirmed=body.confirm)
        try:
            result = adapter.execute(proposed)
        except StalePlanError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        adapter.close()
    audit.record(_audit_path(), result, actor=user, confirmed=body.confirm)
    return dataclasses.asdict(result)


@app.get("/api/audit")
def audit_tail(limit: int = Query(50, ge=1, le=500), _user: str = Depends(require_auth)) -> dict:
    return {"entries": audit.tail(_audit_path(), limit)}


def _login_page(next_path: str = "/", error: str = "") -> str:
    """Render the bundled login template.

    The login page and its stylesheet are same-origin and load no remote assets.

    `next_path` and `error` are both interpolated into markup, so both are escaped —
    `next` comes straight off the query string.
    """
    template = (Path(__file__).parent / "static" / "login.html").read_text(encoding="utf-8")
    for marker in ("<!--ERROR-->", "<!--NEXT-->"):
        if marker not in template:
            # Fail loudly. A silently-unsubstituted marker would render a login form
            # that always posts back to "/" and never shows why a login failed.
            raise RuntimeError(f"login.html is missing the {marker} marker")
    err_html = f'<p class="login-err" role="alert">{escape(error)}</p>' if error else ""
    return template.replace("<!--ERROR-->", err_html).replace(
        "<!--NEXT-->", escape(_safe_next(next_path), quote=True)
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = Query("/")) -> Response:
    """The themed login page. Unauthenticated by definition.

    An already-signed-in visitor is bounced onward rather than shown a form they do
    not need.
    """
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie and _verify_session(cookie):
        return RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
    return HTMLResponse(_login_page(next_path=next))


@app.post("/login")
async def login_submit(request: Request) -> Response:
    """Check the form credentials and mint a session cookie.

    Read with `request.form()` rather than FastAPI's `Form(...)`, but NOT to dodge a
    dependency — an earlier version of this docstring claimed exactly that and was
    wrong. Starlette asserts `python-multipart` is importable before it looks at the
    content type, so both routes need it and it is pinned in `requirements.txt`.
    `request.form()` is simply the smaller surface for three fields read by hand.
    """
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    next = str(form.get("next", "/"))

    if not _credentials_ok(username, password):
        # Deliberately one message for both cases — saying which half was wrong
        # confirms whether a username exists.
        return HTMLResponse(
            _login_page(next_path=next, error="Incorrect username or password."),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    response = RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        SESSION_COOKIE,
        _issue_session(username),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
        path="/",
    )
    return response


def _require_same_origin(request: Request) -> None:
    """Reject cross-origin browser posts to the global logout endpoint."""
    origin = request.headers.get("origin", "")
    parsed = urlsplit(origin)
    host = request.headers.get("host", "").lower()
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != host:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid origin")


@app.post("/logout")
def logout(request: Request, _user: str = Depends(require_auth)) -> Response:
    """Sign out, and actually invalidate the token.

    Clearing the cookie alone only asks the browser to forget it. A token captured
    beforehand would otherwise keep delete access for the rest of its 30 days.
    """
    _require_same_origin(request)
    _revoke_all_sessions()
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=_cookie_secure(),
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/", response_class=HTMLResponse)
def index(_user: str = Depends(require_auth)) -> str:
    """The whole frontend, one file. Behind auth like everything else."""
    return (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
