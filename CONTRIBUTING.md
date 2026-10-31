# Contributing

Use a draft pull request while developing. Drafts run syntax and CI-policy checks;
they do not claim full qualification. The merge check deliberately remains blocked
on a draft, even when its fast checks are green. Mark the PR ready to run every conformance
leg. A later code update requires fresh full qualification before merge.

Full qualification waits for fast checks and uses a native repository-wide queue.
One full request runs at a time. Its heavy matrix is capped, and the cap lets
every leg run at once (at most 20, the account's hosted-runner concurrency): a
run is as long as its slowest job, not its queue. A job longer than the slowest
suite leg is split into shards whose logs a totals job checks as one gate (the
Skyrim mutation gate: `shard`/`shards` matrix rows and `mutations-total`). Other
repositories have independent queues. Started full runs are not automatically
cancelled. Retry failed jobs on the same revision; retain original failures and
passing legs. Do not manually dispatch another copy of an existing PR run.

No push without the repository's local pre-push gate (owner ruling 2026-10-31,
after a month of 4-5 hour rounds: seven of ten red Skyrim runs in one week failed
on something a 5-10 minute targeted set catches at the desk -- moved mutation
anchors, stale corpus rows, pin audits, a test module missing from the pusher's
local set). Each toolkit keeps that set in `scripts/prepush-gate.py` and installs
it as a git `pre-push` hook in maintainers' clones; `--no-verify` is not a
workflow. A red run that is a timing assert on a shared runner is a test-versus-
specification question first, never a re-run: fix the assert to the promise the
code makes, or quarantine it with a written ticket.

Default-branch pushes and manual dispatch still run full qualification. Feature
branches use PR events. The required check verifies dependency outcomes and binds
reports to both the source head and the actual tested PR merge candidate.

The shared CI policy action is development tooling, separate from the runtime
package. Consumers pin a reviewed core commit. Repository-specific commands and
matrices remain in each toolkit; changing the CI action does not change runtime
vendoring. The structural contract intentionally supports a small workflow layout
and refuses unfamiliar shapes rather than silently overlooking a job.

CI artifacts retain source identities, policy hashes, measured test logs, and
per-job queue/execution times. Missing timing metadata is reported as unavailable;
it cannot create a successful qualification result.

Targeted shared policy checks:

    python -B -m unittest discover -s ci/tests -v

The complete runtime conformance matrix runs on CI. Local fixtures do not certify
host enforcement or native game behavior. Test selection, result caching,
parallel test workers, and mutation workspace reuse are not part of this policy.
