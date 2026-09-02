# Librarian contributor instructions

## Scope

Librarian is an authenticated media-deletion service. Treat changes to path handling, authentication, planning, execution, or audit records as security-sensitive.

## Commands

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/pytest -q
node tests/test_jsq.js
python3 -c "import re; s=open('app/static/index.html').read(); open('/tmp/librarian.js','w').write(re.search(r'<script>(.*?)</script>',s,re.S).group(1))"
node --check /tmp/librarian.js
cp .env.example .env
docker compose config --quiet
docker build .
```

## Boundaries

- Never test deletion against a real media library.
- Use temporary directories and fake API clients in tests.
- Never commit credentials, `.env`, audit logs, databases, or media.
- Preserve the plan-before-execute contract and typed confirmation.
- Every path that can be deleted must pass through `resolve_target`.
- A change in any action target must invalidate the plan digest.
- New API routes require authentication unless the security specification names them as public.
- Do not weaken secure-cookie defaults to make local HTTP setup easier. Set `LIBRARIAN_COOKIE_SECURE=false` explicitly for private HTTP deployments.

## Validation

Run the full gate before reporting a release candidate. Report exact results and skipped checks. Auth, path handling, CI credentials, and deletion behavior require an independent review.
