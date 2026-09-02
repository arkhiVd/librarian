# Librarian tasks

## Current phase: public release 1.0

Change class: Risky. The application authenticates users and deletes files. Work occurs only in the isolated public export.

- [x] Create a clean export without the source repository's Git metadata.
- [x] Record the public safety contract, acceptance criteria, and rollback.
- [x] Remove host-specific addresses, paths, names, and production observations.
- [x] Replace production-derived test data with synthetic fixtures.
- [x] Make the frontend standalone.
- [x] Make secure session cookies the default.
- [x] Add example environment and Compose configuration.
- [x] Add README, security policy, license, and contribution guidance.
- [x] Add pinned GitHub Actions, CodeQL, container scanning, and Dependabot configuration.
- [x] Run formatting, lint, Python tests, JavaScript checks, and Compose validation.
- [x] Build and scan the container locally after receiving approval.
- [x] Run Gitleaks against the complete public history.
- [x] Re-read the full diff against `SPEC.md`.
- [x] Obtain an independent read-only review in a fresh Pi/Herdr context.
- [x] Resolve all independent-review findings and rerun the validation gate.
- [ ] Obtain explicit human approval before creating a GitHub repository or pushing.
