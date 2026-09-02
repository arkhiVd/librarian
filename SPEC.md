# Librarian specification

## Problem

Self-hosted media servers often have several ways to add files but no single safe way to remove them. Deleting through one application can leave manager records, hardlinks, source archives, or completed downloads behind.

Librarian provides one authenticated interface for browsing, planning, and executing deletions across music, video, book, and download libraries.

## Safety contract

- Every target passes through a filesystem path jail.
- The library root, paths outside it, symlinks, missing paths, and protected Syncthing names are never valid targets.
- Planning has no side effects.
- Every plan lists files, byte counts, manager actions, warnings, and the required confirmation phrase.
- The plan digest covers filesystem metadata and every planned action. Execution rejects a stale plan.
- The user must type the exact confirmation phrase before execution.
- An audit intent is written before the first destructive action. An outcome follows execution.
- Batch operations collect per-target failures rather than hiding partial completion.
- Authentication fails closed when credentials are absent.
- Browser sessions use HttpOnly, SameSite=Lax cookies. Secure cookies are the default and may be disabled only for a private HTTP deployment.

## Supported integrations

- Lidarr API v1 for music ownership, unmonitoring, and managed-file deletion
- Radarr and Sonarr API v3 for video ownership, unmonitoring, and managed-file deletion
- slskd API v0 for completed-transfer cleanup
- Filesystem-only book deletion, including optional matching source archives

Every integration is optional except the one needed by the selected view. Runtime configuration comes from environment variables.

## Deployment constraints

- Python 3.12
- Container listens on port 8300
- Persistent writable configuration directory for the audit log and session revocation epoch
- Media mounts must be explicit and limited to the intended library roots
- Public internet exposure is unsupported. Use a private network or an authenticated TLS reverse proxy.

## Non-goals

- Managing downloads or adding media
- Repairing media-manager databases
- Multi-user authorization or role-based access control
- Automatic deletion without a preview and typed confirmation
- Deleting the configured library root
- Following symlinks

## Acceptance criteria

1. `ruff format --check .` and `ruff check .` pass.
2. `pytest -q` passes, including path-jail, stale-plan, authentication, audit, and adapter tests.
3. `node tests/test_jsq.js` and `node --check` pass for the browser script.
4. `docker build .` succeeds from the public tree.
5. `docker compose config --quiet` succeeds with example configuration.
6. Gitleaks reports no findings in the working tree or Git history.
7. A repository scan finds no private IP addresses, personal paths, personal documents, credentials, or production data.
8. A demo helper creates only synthetic files and refuses a non-empty destination.
9. The README documents setup, destructive behavior, rollback limits, and supported integrations.
10. CI runs lint, tests, frontend checks, container build, and secret scanning.

## Rollback

This repository is a separate export. Discarding the export rolls back publication work. No command in this phase may deploy, restart, or modify an existing Librarian service.
