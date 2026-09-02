# Contributing

Changes are welcome when they preserve Librarian's deletion safeguards.

## Before opening a pull request

1. Create a branch and keep the change focused.
2. Add or update tests before changing path handling, plan construction, execution, authentication, or audit behavior.
3. Use temporary directories and fake API clients. Never run the test suite against a real library.
4. Run the commands in `AGENTS.md`.
5. Update `SPEC.md` when behavior or security assumptions change.
6. Explain destructive behavior, compatibility changes, and skipped checks in the pull request.

New routes must require authentication unless `SPEC.md` explicitly defines them as public. New delete paths must pass through `resolve_target`. Any field that changes an action or its targets must be covered by the plan digest.

When a production dependency changes, edit `requirements.in` and regenerate the hashed lock with:

```bash
uv pip compile --generate-hashes --output-file requirements.txt requirements.in
uv pip compile --generate-hashes --output-file requirements-dev.txt requirements-dev.in
```

Do not submit credentials, production configuration, audit logs, databases, media, downloaded course material, or screenshots containing personal data.
