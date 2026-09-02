"""Auth tests: /healthz is open, everything else is not, and missing config fails closed."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.main import app

USER = "librarian"
PASSWORD = "correct-horse-battery-staple"


def _header(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("LIBRARIAN_BASIC_USER", USER)
    monkeypatch.setenv("LIBRARIAN_BASIC_PASS", PASSWORD)
    # TestClient uses HTTP. Production defaults to secure cookies, while this explicit
    # opt-out exercises the documented private-HTTP deployment mode.
    monkeypatch.setenv("LIBRARIAN_COOKIE_SECURE", "false")
    # Per-test config dir: logout writes the session-revocation epoch here, and a
    # shared one would let one test's sign-out invalidate another test's session.
    monkeypatch.setenv("LIBRARIAN_CONFIG", str(tmp_path))
    return TestClient(app)


def test_healthz_is_open(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


def test_stylesheet_is_open(client):
    response = client.get("/librarian.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert "--bg:" in response.text


def test_index_requires_credentials(client):
    response = client.get("/")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Basic"


def test_index_accepts_correct_credentials(client):
    response = client.get("/", headers=_header(USER, PASSWORD))
    assert response.status_code == 200


@pytest.mark.parametrize(
    "user,password",
    [(USER, "wrong"), ("wrong", PASSWORD), ("wrong", "wrong"), ("", "")],
)
def test_index_rejects_bad_credentials(client, user, password):
    assert client.get("/", headers=_header(user, password)).status_code == 401


def test_unconfigured_service_fails_closed(monkeypatch):
    """No credentials configured must break the service, not open it."""
    monkeypatch.delenv("LIBRARIAN_BASIC_USER", raising=False)
    monkeypatch.delenv("LIBRARIAN_BASIC_PASS", raising=False)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/healthz").status_code == 200
    assert client.get("/").status_code == 500


# ── cookie sessions (Phase 3) ────────────────────────────────────────────────
# Basic auth above is unchanged and still supported; these cover the themed login
# page added alongside it.

HTML = {"Accept": "text/html,application/xhtml+xml"}
SAME_ORIGIN = {"Origin": "http://testserver"}


def _login(client, user=USER, password=PASSWORD, next_path=None):
    body = {"username": user, "password": password}
    if next_path is not None:
        body["next"] = next_path
    return client.post("/login", data=body, follow_redirects=False)


def test_every_route_except_the_open_ones_requires_auth(client):
    """Walk the app's own route table rather than naming endpoints by hand.

    Written after a review demonstrated that authentication could be deleted from
    destructive endpoints while all existing tests stayed green. Nothing asserted
    those routes were protected because most adapter tests do not use TestClient.

    Listing routes explicitly would have the same blind spot, because a new endpoint
    would simply not be listed. Enumerating from `app.routes` means a route added
    tomorrow is covered today, and a route deliberately left open has to be named
    here.
    """
    from fastapi.routing import APIRoute

    open_paths = {"/healthz", "/librarian.css", "/login"}
    checked = 0
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.path in open_paths:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            # No credentials, and Accept: */* so we get a status rather than a
            # redirect. A 401 means the dependency ran. Anything else — including a
            # 422 for a missing body — means the request got past authentication.
            response = client.request(method, route.path, follow_redirects=False)
            assert response.status_code == 401, (
                f"{method} {route.path} returned {response.status_code}, not 401 — "
                f"it is reachable without credentials"
            )
            checked += 1

    # Guard against the enumeration silently matching nothing.
    assert checked >= 10, f"only {checked} routes checked; the walk is not finding them"


def test_login_page_is_open(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert 'name="password"' in response.text
    # The unsubstituted markers must never reach the browser.
    assert "<!--NEXT-->" not in response.text
    assert "<!--ERROR-->" not in response.text


def test_pages_use_only_the_bundled_stylesheet(client):
    for response in (
        client.get("/login"),
        client.get("/", headers=_header(USER, PASSWORD)),
    ):
        assert 'href="/librarian.css"' in response.text
        assert "http://" not in response.text
        assert "https://" not in response.text


def test_login_page_substitutes_each_marker_exactly_once(client):
    """Substitution is a plain str.replace(), so a marker written anywhere else in the
    template — including inside an explanatory comment — is also replaced. That leaked
    documentation text into the rendered page and closed the comment early."""
    response = client.post(
        "/login", data={"username": USER, "password": "wrong"}, follow_redirects=False
    )
    assert response.text.count('class="login-err"') == 1
    assert response.text.count('name="next"') == 1


def test_browser_navigation_is_redirected_not_challenged(client):
    """The whole point of this phase: no native basic-auth popup for a browser."""
    response = client.get("/", headers=HTML, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "WWW-Authenticate" not in response.headers


def test_api_call_still_gets_a_plain_401(client):
    """fetch() and curl send Accept: */* and must get a status, not a login page."""
    response = client.get("/api/audit", follow_redirects=False)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Basic"


def test_login_sets_a_session_cookie_that_authenticates(client):
    response = _login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    cookie = response.cookies.get("librarian_session")
    assert cookie
    set_cookie = response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.replace("samesite", "SameSite")
    assert "Secure" not in set_cookie

    assert client.get("/", headers=HTML).status_code == 200


def test_session_cookie_is_secure_by_default(client, monkeypatch):
    monkeypatch.delenv("LIBRARIAN_COOKIE_SECURE")
    response = _login(client)
    assert "Secure" in response.headers["set-cookie"]


def test_session_cookie_accepts_common_true_values(client, monkeypatch):
    for value in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("LIBRARIAN_COOKIE_SECURE", value)
        assert "Secure" in _login(client).headers["set-cookie"]


def test_invalid_cookie_security_setting_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("LIBRARIAN_BASIC_USER", USER)
    monkeypatch.setenv("LIBRARIAN_BASIC_PASS", PASSWORD)
    monkeypatch.setenv("LIBRARIAN_CONFIG", str(tmp_path))
    monkeypatch.setenv("LIBRARIAN_COOKIE_SECURE", "sometimes")
    local_client = TestClient(app, raise_server_exceptions=False)
    response = local_client.post(
        "/login",
        data={"username": USER, "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 500


def test_bad_login_returns_the_form_with_an_error_and_no_cookie(client):
    response = _login(client, password="wrong")
    assert response.status_code == 401
    assert "librarian_session" not in response.cookies
    assert "Incorrect username or password." in response.text


def test_logout_requires_authentication(client):
    response = client.post("/logout", headers=SAME_ORIGIN, follow_redirects=False)
    assert response.status_code == 401


def test_logout_rejects_cross_origin_requests(client):
    _login(client)
    response = client.post(
        "/logout",
        headers={"Origin": "https://attacker.example"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert client.get("/api/audit").status_code == 200


def test_logout_clears_the_session(client):
    _login(client)
    assert client.get("/", headers=HTML).status_code == 200
    response = client.post("/logout", headers=SAME_ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.get("/", headers=HTML, follow_redirects=False).status_code == 303


def test_logout_invalidates_a_token_captured_beforehand(client):
    """A token copied before logout must not retain delete access."""
    token = _login(client).cookies["librarian_session"]
    client.post("/logout", headers=SAME_ORIGIN, follow_redirects=False)

    client.cookies.set("librarian_session", token)
    assert client.get("/api/audit", follow_redirects=False).status_code == 401


def test_logging_in_again_after_logout_works(client):
    """Revocation is by issue time, so a fresh login must not be caught by it."""
    _login(client)
    client.post("/logout", headers=SAME_ORIGIN, follow_redirects=False)
    client.cookies.clear()

    response = _login(client)
    assert response.status_code == 303
    client.cookies.set("librarian_session", response.cookies["librarian_session"])
    assert client.get("/api/audit", follow_redirects=False).status_code == 200


def test_forged_and_tampered_cookies_are_rejected(client):
    good = _login(client).cookies["librarian_session"]
    user_b64, issued, expiry, sig = good.split(".")
    for bad in [
        "garbage",
        "a.b.c",
        "a.b.c.d",
        good[:-4] + "AAAA",  # broken signature
        f"{user_b64}.{issued}.{int(expiry) + 99999}.{sig}",  # extended expiry, stale sig
        f"{user_b64}.{int(issued) + 99999}.{expiry}.{sig}",  # forward-dated issue time
        "",
    ]:
        client.cookies.set("librarian_session", bad)
        assert client.get("/api/audit", follow_redirects=False).status_code == 401
    client.cookies.clear()


def test_expired_cookie_is_rejected(client, monkeypatch):
    from app import main

    expired = main._issue_session(USER, ttl=-1)
    client.cookies.set("librarian_session", expired)
    assert client.get("/api/audit", follow_redirects=False).status_code == 401


def test_rotating_the_password_invalidates_outstanding_sessions(client, monkeypatch):
    token = _login(client).cookies["librarian_session"]
    monkeypatch.setenv("LIBRARIAN_BASIC_PASS", "a-new-password")
    client.cookies.set("librarian_session", token)
    assert client.get("/api/audit", follow_redirects=False).status_code == 401


@pytest.mark.parametrize(
    "hostile",
    ["//evil.example", "https://evil.example/x", "http://evil.example"],
)
def test_next_cannot_redirect_off_site(client, hostile):
    """`next` is attacker-supplied — it must never become an off-site redirect."""
    response = _login(client, next_path=hostile)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_next_survives_a_legitimate_path(client):
    response = _login(client, next_path="/api/audit")
    assert response.headers["location"] == "/api/audit"


@pytest.mark.parametrize("field", ["username", "password"])
def test_non_ascii_login_input_is_rejected_not_a_500(client, field):
    """`secrets.compare_digest` raises TypeError on a non-ASCII `str`.

    Basic auth never reached it — that header decodes as ASCII and fails earlier — but
    the login form feeds it raw user input, so `username=ü` returned 500 and sprayed
    tracebacks into the service log for any unauthenticated caller.
    """
    body = {"username": USER, "password": PASSWORD, field: "üé中"}
    response = client.post("/login", data=body, follow_redirects=False)
    assert response.status_code == 401


def test_non_ascii_configured_password_still_permits_login(client, monkeypatch):
    """The same bug made a non-ASCII configured password impossible to log in with."""
    monkeypatch.setenv("LIBRARIAN_BASIC_PASS", "pässwörd-ünïcode")
    response = client.post(
        "/login",
        data={"username": USER, "password": "pässwörd-ünïcode"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_redirect_preserves_the_query_string(client):
    response = client.get("/api/audit?limit=7", headers=HTML, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/api/audit%3Flimit%3D7"


def test_next_is_escaped_into_the_form(client):
    """`next` lands in a value="" attribute, so a quote must not break out of it."""
    response = client.get('/login?next=/"><script>alert(1)</script>')
    assert "<script>alert(1)</script>" not in response.text
