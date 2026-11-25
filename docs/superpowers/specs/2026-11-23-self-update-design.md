# Self-update from GitHub: design (draft for approval)

Status: APPROVED 2026-11-23 by the user ("ok continue", after the design and the core-scope recommendation were put to them). Nothing is built.

Update 2026-11-23: the check and the verified fetch were built by another session as core PR #13 (`release.py`). This design's remaining scope is the guarded apply half (second go for guard changes, diffs, setup and canary, automatic rollback, fail-closed marker); the plan builds it on top of #13. Where this document differs from #13 (the off switch is `updates.json`, not `config.json`; the toolkit supplies a profile in code, not a downloaded manifest), #13 wins.
Applies to: the shared core (`modding_harness`), then each toolkit (X4, Skyrim) as a consumer.

## Intent and why

Users find a toolkit on Nexus and download it there once. Every update after that comes from GitHub, through
the user's own agent. The user's words (2026-11-23, relayed by the Skyrim session and confirmed to this session
the same day): "they'll find the mod on Nexus download there And then they'll update from github", and the
same for the X4 repo and the core repo.

Today nothing announces a new version, and updating is manual: X4 re-runs its installer with `--over-existing`;
Skyrim has five manual steps (README "How do I update the toolkit?"). Nexus lags GitHub (the live X4 page was
on 4.1.0 while GitHub was at 4.3.0).

## Goals

1. A session-start notice that a newer release exists, once per new version, never blocking.
2. One agent-run update path, built once in the core, that downloads the release asset from GitHub, verifies
   it, stages it for review, applies it on the user's go, runs the toolkit's setup and hook canary, and reports
   what it kept aside.
3. Each toolkit supplies only its own facts: release repo, asset name, setup and canary commands, notes files.
4. The Nexus page and each README say it plainly: first download on Nexus, updates from GitHub through the agent.

## Non-goals

- No automatic apply, no background timer, no scheduled task, no telemetry.
- No downgrade command (rollback of the last update stays the existing `upgrade-rollback`).
- No Nexus API use; no updating of game mods or of anything but the toolkit install.
- No signing scheme (see Limits). No support for installs without a manifest.

## Anti-goals (a failure even if it technically works)

- An update that replaces guard or hook code without the user having seen exactly what changes (user decision:
  option A, 2026-11-23).
- A check that delays a session, blocks offline, prints repeatedly, or sends anything but one anonymous request.
- The downloaded release choosing its own source repo or its own setup/canary commands.
- A canary failure that leaves the agent running with broken or missing guards.

## Constraints (READ from the repo unless marked)

- The core already has journalled upgrades: `upgrade-preview`, `upgrade-stage`, `upgrade-apply`,
  `upgrade-rollback` (`modding_harness/installation.py`, `upgrade.py`). They take version-1 hash inventories,
  keep local files (`LOCAL_ONLY`, `notes_files`), refuse on custom pre-tool handlers, and journal for rollback.
  The updater drives these; it does not replace them.
- Apply needs `--review-sha256` of the exact `complete.json` reviewed, and `--quiescent` (the caller asserts no
  host or editor writes). The core cannot tell who typed a "go"; this is the same limit the upgrade path has today.
- User rules: downloads go into their own new empty directory and are untrusted; any Python reading them runs
  with `-I`; no personal details in any outside request; guard code deploys only after review and passing hook
  tests (CI on the tagged release is the release gate, not a substitute for the user seeing the guard diff).
- GitHub unauthenticated API allows 60 requests per hour per IP (INFERRED from GitHub's published limit, to be
  measured); the check must survive a 403 silently.

## Approaches considered

1. **One updater in the core driving the existing upgrade path. CHOSEN (user: "your pick").**
   Thin front end (check, download, verify, unpack aside) and back end (setup, canary, report) around
   `upgrade-stage` / `upgrade-apply`. Cost: network code in the core, a re-pin and a manifest per toolkit.
   Gain: one tested implementation; safety (local-file preservation, collisions, rollback) already exists.
2. Each toolkit keeps its own updater; the core ships only the version check. Cheap, but three updaters drift
   and the user still downloads and reviews by hand.
3. Prompt only: README/Nexus give an "ask your assistant to update" prompt. This is today's Nexus text; nothing
   announces a version and verification depends on the agent improvising.

## Design

### Manifest (per toolkit, in the INSTALLED copy)

`.modding-agent-harness/update.json`, shipped by the toolkit's installer and read from the installed copy only:

```
{"v": 1, "repo": "OWNER/NAME", "asset": "Toolkit-v{version}.zip",
 "version_file": "<installed path the version is read from>",
 "setup": ["<argv>"], "canary": ["<argv>"], "notes_files": ["X4-NOTES.md"],
 "optional_assets": []}
```

The release repo and the commands come from the installed manifest, never from the downloaded asset. If the new
release carries a manifest that differs in `repo`, `setup` or `canary`, that difference is shown in the review
and applying it needs the same go as a guard change. Skyrim lists `ToolkitProbe.zip` under `optional_assets`; it
is never fetched unless the user asks for it.

### Version check (`update-check`, called from each toolkit's session-start hook)

- One unauthenticated GET of `https://api.github.com/repos/<repo>/releases/latest`, hard 3 s deadline.
  No installed version, path or identifier is sent; the User-Agent is a generic client name.
- Result cached in `.modding-agent-harness/update-check.json` (local-only, gitignored): one request per 24 h on
  success, per 6 h after a failure. A version is announced once, then recorded as announced.
- Ignores drafts and prereleases. Installed newer than latest, or any parse failure: silent.
- Off switch: `"update_check": false` in `.modding-agent-harness/config.json` or environment
  `MODDING_HARNESS_NO_UPDATE_CHECK=1`. Offline, 403, timeout: silent, nothing printed, nothing blocked.
- Output is one line: installed version, latest version, and "ask your agent to update from GitHub".

### Updater (`update`, two steps, the user's go between them)

Step 1, `update` (never applies):
1. Read the manifest; fetch the release metadata; pick the exact asset name; refuse if absent.
2. Download into a new empty directory under the install's state folder (https only; redirects only to
   GitHub's own download hosts; size cap).
3. Verify: sha256 of the bytes must equal the digest GitHub publishes for that asset. No digest published ->
   refuse (TLS alone is not enough). Unpack aside with traversal, absolute-path, symlink and entry/size caps.
4. Build the candidate inventory from the unpacked tree (sha256 per file) and use the recorded inventory of the
   installed version as `--previous` when one exists; otherwise installed bytes are unknown and every changed file
   is a preserved collision (safer, a longer review).
5. Call `upgrade-stage`. Print the review. Guard, hook, settings and manifest files (the toolkit supplies the
   path list) are shown as unified diffs, installed bytes against candidate bytes, under their own heading; every
   other changed file is listed with its status and hash only. Also printed: collisions, manifest differences,
   the review hash, and a closing `next:` block telling the agent exactly what to show the user and which command
   follows. `application_authorized` stays false.

Step 2, `update --apply REVIEW_SHA256 [--accept-guard-changes]` (only after the user says go):
1. If the review contains any guard, hook, settings or manifest change, apply refuses unless
   `--accept-guard-changes` is also given. That makes the second go an explicit, separate argument the agent has to
   name; it does not prove a human typed it (see Limits).
2. Refuse if the upgrade lock is held (another updater) or another session reports itself active; the lock is the
   existing `upgrade.lock`. The updating agent is itself the host that loads the hooks being replaced, so the
   caller must be the only writer (`--quiescent`); the report says so.
3. Pre-update snapshot is the existing rollback journal; call `upgrade-apply` with `--quiescent`.
4. Run `setup`, then `canary`. When the new release's manifest changes `setup` or `canary`, both the installed
   (old) and the new commands must pass; trust in the new commands is complete only from the next update on.
5. Any setup or canary failure -> automatic `upgrade-rollback`, then the OLD canary again on the restored state;
   report both. Setup side effects outside the journal (generated or deployed files) are listed in the report as
   not restored. (My calls.) If the post-rollback canary also fails, fail closed: write
   `.modding-agent-harness/update-failed.json`; the session-start hook then prints it loudly every session, and
   `update` refuses until the user clears it.
6. Success -> record the new inventory and version. Report: what changed, the operation id, and what was kept
   aside (collisions, preserved local files).

What the inventory covers: the install root as unpacked from the asset, byte for byte. Files a toolkit's setup
generates or deploys elsewhere (X4: the game-root `.claude/` copy) are setup's output, outside the inventory, and
are covered by that toolkit's own session-start drift check, not by the updater.

### Wording

Each README first-use section and each Nexus description say: first download on Nexus; updates come from GitHub
through the agent, and the session-start line tells you when one exists. Each toolkit session owns its own text.

## Rollout order

1. Core PR #12 (launcher, `notes_files`) merges first: the updater needs `notes_files`.
2. Core PR: `selfupdate.py`, `update-check`, `update`, tests, README, CONTRIBUTING pointer.
3. X4 4.3.1: re-pin, `update.json`, session-start line, README and Nexus wording, `agent-harness.py` pass-through.
4. Skyrim: same, by the Skyrim session; its `ToolkitProbe.zip` as optional asset.
Releases stay gated on a green CI run per job on the exact commit.

## Testing (what would have made it go red)

Offline against a loopback fake server through an injectable fetch seam: success; offline; timeout; 403 rate
limit; digest missing; digest mismatch; wrong asset name; redirect to another host; traversal, absolute-path and
symlink entries; oversized asset; draft and prerelease ignored; installed newer; malformed JSON; cache hit within
24 h and failure backoff; announced-once; off switch honoured by both config and environment; the hook never
exceeds its deadline; manifest from the asset cannot change the repo; canary failure restores. Mutation twins on
each refusal, per the core's existing practice. Windows and Linux CI. One read-only live check against the public
v4.3.0 release to measure real redirect and digest behaviour (UNMEASURED today).

## Limits (stated honestly)

- Verification is GitHub's own digest. A compromised repo account compromises asset and digest together; stopping
  that needs release signing, which is out of scope.
- The apply step cannot prove a human typed the go; the review hash binds the apply to exactly what was shown.
- A first update from an install that never recorded an inventory is a longer review, not an error.

## Left to the builder

Exact module and CLI names; manifest filename and location refinements; cache file format; the "guard/hook/
settings" path classification list per toolkit (each toolkit supplies it); exit codes (follow the upgrade CLI:
2 refusal, 1 partial failure); how setup/canary argv are run (no shell, a timeout, output captured).

## Scope of "the core repo" (approved as proposed)

The core reaches users vendored inside each toolkit release (`scripts/vendor.py`), not as its own install, so it
needs no self-update of its own: a toolkit update brings the new core with it. If the core is ever installed on
its own, it adds a manifest of its own and nothing else changes.

## My calls (check these)

- Review shows diffs for guard/hook/settings/manifest files and hashes for the rest; a separate
  `--accept-guard-changes` argument is the second go.
- Setup failure rolls back like a canary failure; both old and new canary must pass when the manifest changes.
- Fail closed after a failed rollback canary: a marker file, a loud session-start line, no further updates.
- The CLI output is the agent's contract (a `next:` block); no separate skill is required, each toolkit may add one.
- The existing upgrade lock plus a refusal when another session is active; no new locking scheme.
- Auto-rollback on canary failure instead of leaving the user to run it.
- Manifest read from the installed copy only; differences in a new release's manifest need the guard-level go.
- Refuse when GitHub publishes no digest.
- 24 h / 6 h cache windows and the 3 s deadline.
- Optional assets are never downloaded by default.
