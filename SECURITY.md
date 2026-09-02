# Security policy

## Supported versions

Security fixes target the latest release on the default branch.

## Reporting a vulnerability

Do not open a public issue for a vulnerability that could permit authentication bypass, path-jail escape, arbitrary deletion, credential disclosure, or plan tampering. Use GitHub's private vulnerability reporting for this repository.

Include the affected version, reproduction steps using disposable files, expected behavior, and observed behavior. Do not include real credentials, library paths, media names, or audit records.

## Deployment requirements

Librarian is an administrative tool with permanent delete access.

- Do not expose it directly to the public internet.
- Bind it to localhost or a private network and place it behind an authenticated TLS reverse proxy when remote access is needed.
- Keep `LIBRARIAN_COOKIE_SECURE=true` with HTTPS.
- Set `LIBRARIAN_COOKIE_SECURE=false` only for direct HTTP access on a trusted private network.
- Use a unique, long password. HTTP Basic and the browser login share this credential.
- Restrict API keys to the minimum permissions supported by each manager.
- Mount only intended library roots. Do not mount `/`, `/home`, a Docker socket, or a broad storage parent.
- Back up data before enabling deletion. Test with disposable files first.
- Protect `/config`, which contains audit records and session-revocation state.
- Review the plan targets and warnings before typing the confirmation phrase.

## Trust boundaries

File names and manager metadata are untrusted input. The frontend escapes values for their HTML or JavaScript context. The backend resolves all delete targets through `app.scan.resolve_target` and refuses symlinks.

The path jail limits deletion to configured roots, but it cannot recover a file after a valid deletion. Filesystem snapshots, manager recycle bins, and backup policies remain the operator's responsibility.

The audit log records intent and outcome. An intent without a matching outcome means execution may have stopped partway through.
