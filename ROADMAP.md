# Librarian roadmap

## Public release 1.0

Outcome: a portable, documented repository that can be reviewed and tested without access to the original homelab.

- Replace host-specific configuration and design-system dependencies.
- Add standalone styling and secure-cookie configuration.
- Replace production-derived examples with synthetic fixtures.
- Add Compose and environment examples.
- Add README, security policy, license, CI, Dependabot, and secret scanning.
- Run the full local gate and an independent review.

Exit criteria are the acceptance criteria in `SPEC.md`. No GitHub push is part of this phase.

## Later work

- Add mock manager APIs so the demo can exercise music and video plans.
- Publish versioned container images.
- Add integration tests against disposable mock Lidarr, Radarr, Sonarr, and slskd services.
- Add per-user authorization only if a real multi-user use case appears.
