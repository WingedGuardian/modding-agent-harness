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

## CI gates releases, not local work

This rule is the same in the shared core and in every toolkit that pins it.

- CI on GitHub gates releases only: a merge to `master`, a tag, a GitHub release, a version bump, a new core pin in a toolkit. Nothing is released on a red or missing run, read per job on the exact commit.
- Nothing local waits for CI: trying a change in your own install or toolkit checkout, deploying tools, docs, skills and guard/hook code there, continuing work, or reporting work done ("CI pending").
- Guard and hook code reaches a game install after an independent review of the change and the local hook tests passing on that commit; the deployer's backup is the rollback. Never deploy unreviewed guard code.
- Push a branch once per batch, never just to see CI, and never wait for its result before the next step. A CI failure is fixed in the next batch.
- The local pre-push gate stays a quick sanity check; full suites, mutation witnesses and slow legs run on CI.
