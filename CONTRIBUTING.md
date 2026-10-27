# Contributing

Use a draft pull request while developing. Drafts run syntax and CI-policy checks;
they do not claim full qualification. The merge check deliberately remains blocked
on a draft, even when its fast checks are green. Mark the PR ready to run every conformance
leg. A later code update requires fresh full qualification before merge.

Full qualification waits for fast checks and uses a native repository-wide queue.
One full request runs at a time, with at most four heavy matrix jobs. Other
repositories have independent queues. Started full runs are not automatically
cancelled. Retry failed jobs on the same revision; retain original failures and
passing legs. Do not manually dispatch another copy of an existing PR run.

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

Reports exclude authentication and raw pull-request text. The API credential is
used only for metadata requests; retained reports contain source identities and
job results, not the request environment or event body.

Targeted shared policy checks:

    python -B -m unittest discover -s ci/tests -v

The complete runtime conformance matrix runs on CI. Local fixtures do not certify
host enforcement or native game behavior. Test selection, result caching,
parallel test workers, and mutation workspace reuse are not part of this policy.
