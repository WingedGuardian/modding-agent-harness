# Shared harness contributor guidance

## Game launch approval

Reconfirm immediately before launch if preparation or other work delayed an earlier approval; consent is time-sensitive.

Before every agent-initiated game launch or restart, ask for approval of that specific next launch and wait for the user's answer. This includes normal, menu-only, headless, private/background, probe, smoke and E2E runs.

Name the game, mode, desktop/focus impact and test profile/save. For each headless session, ask which desktop to use; never carry a prior session's choice forward.

Development, deployment or test-plan approval, batches of tests, resource exemptions, previous launches and "continue" do not authorize a launch. No automatic retries or launches hidden inside test scripts. Without launch-specific confirmation, continue offline work.

- Develop changes in draft pull requests. Draft CI is fast feedback, not full qualification.
- Mark a PR ready only when the change is ready for full conformance. New code on a ready PR requires fresh qualification.
- Keep full suites on GitHub CI; run only focused checks locally. Preserve test isolation and fail-closed outcomes.
- Stage explicit paths, preserve existing line endings, and commit finished verified units.
- CI development tooling lives under `ci/`; runtime vendoring remains separate.
- Pin external actions and shared CI components to immutable commit SHAs.
- Preserve required platforms, mutation witnesses and refusal paths when changing workflow structure.
