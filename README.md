# Modding agent harness

An offline Python 3.10–3.13 library shared by independently released game toolkits.
Game profiles own save membership, protected roots, rules and validators. Host
adapters own payload interpretation and transport. Neither belongs in preservation.

The v1 contracts distinguish policy, evaluation, preservation and validation.
Unknown tools warn and allow, with unguarded coverage. A profile that declares save
obligations must complete evaluation; an unguarded result refuses that work. Local
failed/partial restrictions also refuse, including permission prompts. Known malformed requests
must be reported as failed. Installation does not establish host hook trust.
`Dispatcher.pre` returns a deny immediately. An ask, from the profile or a local
restriction, can run on a human "yes", so it first passes the save-scope,
protected-store and typed-approval checks (any refusal wins over the ask) and has
its saves preserved; it is then returned with `preservation="verified"` and the
transaction ids in `Outcome.transactions`.
OpenCode profiles can supply `createGuard({isJudgedTool: tool => ...})` for
additional tools their adapter recognizes. It returns a boolean; classifier or
transport failure refuses those known calls. Other tools retain the unknown-tool
warning and allow behavior even when the receipt service is unavailable.

`SaveStore` records complete logical saves in immutable UUID transactions under
`.modding-agent-harness/backups/saves/`, retains backups permanently, verifies
source and stored bytes, and treats indexes as unnecessary. Directory fsync is
used on supporting POSIX filesystems; Windows CRT offers no directory equivalent.
Ordinary copying cannot atomically snapshot a concurrent native save writer.
Windows save and store comparisons recognize resolved extended drive and UNC
spellings; preservation retains the caller's physical path spelling for I/O.
Older transaction identity keys remain usable for selected recovery and source
revalidation; reading them never rewrites their immutable manifests.
In-place recovery requires a quiescent writer, backs up the complete current save,
and journals individual publications; publication of a pair is not atomic.
Entirely absent groups are rechecked independently of the pre-restore backup.
Every previously absent destination uses no-clobber publication; a counterpart
appearing during publication produces an accurately journalled partial failure.

Run fixtures without installation or network:

    python -B -m unittest discover -s tests -v

Core fixtures do not certify host enforcement or native game round-trips.

The generic adapter contract works without `.claude`, `.codex`, or `.opencode`.
Run `python -B -m modding_harness.generic --workspace PATH capabilities` to see
its actual coverage. `toy-check --save-root PATH` reads a version 1 `Action`
JSON object on stdin and applies the toy third-game file policy. `--preview`
never preserves bytes or records an execution receipt. Game toolkits provide
their own policy callback to `generic.handle`; the toy policy is an example,
not a replacement for game rules. `examples/adapter.py` describes the host side.

`coverage --session-id ID` reports receipt correlation and every unknown tool
occurrence. Commands, arguments, paths and environment are excluded; session
and call IDs are hashed. Missing/delayed post events, unsupported interception,
and unavailable logging remain distinct from successful checks. Source and
trust labels are caller declarations. Sparse repeat notices never discard the
underlying unknown occurrence count.

`upgrade.preview` compares a candidate hash inventory with installed bytes and
an optional verified previous inventory. It writes nothing: unchanged files and
verified owned replacements are distinguished from modified/unowned collisions,
local files are preserved, and retired files are retained. It never guesses which
notes belong to the toolkit. `merge_settings` prepares hook configuration while
retaining custom handlers and unrelated settings. Custom pre-tool handlers are
flagged for ordered-dispatch review; host hook-list order is not synchronization.

`stage_review_bundle` writes an independent private review directory containing
verified candidate bytes, installed rollback bytes, a conservative file plan and
merged Claude and Codex project settings. It also checks local settings for custom pre-handlers;
those restrictions cannot be overlooked just because project settings are clean.
Installed instructions and local notes remain unchanged. A bundle may contain
private settings and notes: keep it outside public artifacts. Its completion
marker proves staging finished; it grants no deployment, rollback or host trust.

`installation.apply` consumes the exact reviewed bundle hash and explicit
collision choices, preserves every replaced file in a permanent upgrade journal,
and publishes runtime files before hook definitions. Local configuration and
personal notes cannot be replaced; unresolved custom pre-handlers refuse the
entire upgrade. Late configuration changes invalidate the review. Knowledgebase
updates independently retain legacy notes before replacement.
Selected candidate modes without write permission refuse during preflight,
before any installed change; installing them would block the promised rollback.
Keeping an existing file does not require changing its candidate's permissions.
`installation.rollback` checks both bytes and publication identity, restores old
files and retains newly created files in its journal. Neither operation changes
host trust. Stop host/editor writers first: the upgrader lock coordinates other
upgraders, and expected-byte checks are not a portable atomic compare-and-swap
against arbitrary concurrent writers. New-file publication is no-clobber.
Read-only targets refuse the entire operation during preflight without lifting
their protection. A later permission or ACL change can still cause a journalled
partial failure; rollback must be reviewed against that retained evidence.

A profile may provide a qualified `review_builder` for `upgrade-stage
--integrate-local-policy`. It proposes narrowly scoped JSON settings edits,
retains exact original policy sources, and binds required candidate runtime
hashes. Staging never applies the proposal. Application reproduces it with the
profile validator and requires an explicit `reviewed_settings` choice for every
proposed file, in addition to the review hash and quiescence declaration. The
runtime is published before the local registration and hook settings. Complete
pre-tool groups must match the candidate; unknown handlers, changed matchers,
timeouts or async fields refuse. Other local configuration and notes remain
protected. Rollback refuses changed retained policy sources or runtime files.
These checks establish migration integrity, not policy equivalence or host trust;
each game profile must qualify its bridge before enabling this migration.

Profiles can supply `candidate_preparer` for `upgrade-stage --render-config FILE`
and `candidate_validator` for application. Preparation renders only independently
copied, inventoried candidate files, leaves the source and install unchanged, and
binds the resulting bytes to the preview and review hash. Unknown paths, oversized
outputs, changed inputs or failed readback leave no completion marker. Original
source hashes are retained separately. Application runs the profile validator
before its first installed write, so unresolved destination placeholders can
refuse cleanly. The core does not infer game paths or perform host configuration.

Profiles may explicitly declare generation of `.codex/hooks.json` when the
installer normally creates it from a source template. Its prior bytes, including
absence, are observed before rendering; new bytes enter the same review, collision
and rollback rules as copied files. This mechanism cannot generate local notes,
personal settings or recovery files. Existing Claude and Codex definitions use
`merged_settings`, retaining unrelated fields and post handlers. A retained custom
pre-tool handler in either file requires ordered integration, even when that file
is omitted from the requested update. A familiar POSIX command does not establish
ownership of a different `commandWindows` policy. Older bundles lacking the
combined observations must be staged again.

Pre-tool ownership compares complete, type-preserving JSON groups, including
matcher and handler options. Exact current candidate groups qualify on repeat;
an installed settings file matching its verified previous hash also qualifies
its complete old groups. Otherwise provide `--owned-pre-groups FILE`, containing
`{"v":1,"files":{".claude/settings.json":[...]}}` (or `.codex/hooks.json`), from a
verified prior definition. References apply only to their named host file.
`--owned-command` alone cannot retire a changed pre-tool group; it remains useful
for other lifecycle handlers. Unknown pre-tool policies require ordered review.

New metadata uses complete descriptor writes, fsync and independent size/hash
readback before no-clobber publication. Incomplete or corrupted transaction,
review and rollback-plan staging cannot become authoritative. Save preservation
also retains its separate final manifest/blob verification.
