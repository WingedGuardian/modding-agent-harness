# Shared harness contributor guidance

- Develop changes in draft pull requests. Draft CI is fast feedback, not full qualification.
- Mark a PR ready only when the change is ready for full conformance. New code on a ready PR requires fresh qualification.
- Keep full suites on GitHub CI; run only focused checks locally. Preserve test isolation and fail-closed outcomes.
- Stage explicit paths, preserve existing line endings, and commit finished verified units.
- CI development tooling lives under `ci/`; runtime vendoring remains separate.
- Pin external actions and shared CI components to immutable commit SHAs.
- Preserve required platforms, mutation witnesses and refusal paths when changing workflow structure.
