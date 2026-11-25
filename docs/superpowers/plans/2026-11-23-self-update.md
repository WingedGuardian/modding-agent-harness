# Guarded update (apply half) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On top of core PR #13 (`release.py`: check and verified fetch), add a code-enforced, reviewed apply: guard-change second go, unified diffs of guard files, setup then canary, automatic rollback, and a fail-closed marker.

**Architecture:** One new module `modding_harness/guarded_update.py` (plus a small `update_review.py` for rendering). It wraps `release.fetch`, `upgrade.stage_review_bundle`, `installation.apply` and `installation.rollback`, and replaces none of them. The toolkit supplies an `UpdatePolicy` next to its `ReleaseProfile`; both are code in the INSTALLED toolkit, so the downloaded release cannot choose its own repo, setup or canary.

**Tech Stack:** Python >= 3.10, standard library only (`difflib`, `subprocess`, `json`), pytest.

**Spec:** `docs/superpowers/specs/2026-11-23-self-update-design.md` (approved 2026-11-23; its fetch half is now PR #13, this plan is the rest). Read `modding_harness/release.py` first.

**Base:** branch `feat/guarded-update` from `origin/feat/self-update` (PR #13, head 165a1d4, stacked on #12 which merged as 6148db3). Do NOT push to `feat/self-update`; its owner is the Skyrim session. Retarget to `master` after #13 merges.

## Global Constraints

- Standard library only; Python >= 3.10.
- Repo, zip name and installed version come from `release.ReleaseProfile`; setup and canary argv, guard globs, render-config and policy files come from `UpdatePolicy`; never from the downloaded release.
- `apply` refuses without the review SHA-256 of the exact `complete.json`; if the review holds any guard, hook, settings or policy-file change it ALSO refuses without `accept_guard_changes=True` (CLI `--accept-guard-changes`).
- Work folders are outside the install (`release.updates_home`); the reviewed upgrader refuses a bundle inside the install root.
- Every collision and every key of `merged_settings_hashes` needs an explicit choice (`keep`, `candidate`, `merged_settings`); render-config is mandatory (the apply validator refuses unrendered hook files).
- Setup and canary run as argv without a shell, cwd = install root, timeout 600 s, output captured; argv may contain spaces and `(x86)`.
- Exit codes follow the upgrade CLI: 0 ok, 2 refusal, 1 partial failure.
- Tests are offline (`release` tests refuse `urlopen`; reuse its fake `http`). Full suites run on CI; locally only the new tests, through the resource wrapper.

## Review Focus

- The new release changes a policy file (setup or canary definitions): old AND new canary must pass; the diff is shown and needs the guard go.
- A canary that passes only because the setup step failed silently (exit 0, nothing done): the plan's setup/canary must be checked by exit code AND a captured-output tail in the report, and a missing executable counts as failure, not skip.
- Rollback itself fails or is interrupted: marker still written, report names the operation id, and the next `update-stage` refuses until `update-clear-failed`.
- Collision rows whose `candidate_sha256 == previous_sha256` (new release did not touch the file): shown as "unchanged upstream, keep is lossless", never as a guard change by mere path match.
- A guard glob that matches a file only present in the candidate (new hook) or only installed (removed hook): both listed and both need the guard go.
- Two updaters at once, or an agent session still writing: lock-held and not-quiescent are refusals with a clear message, never a partial apply.

---

### Task 1: Policy, classification and review rendering

**Files:**
- Create: `modding_harness/update_review.py`
- Test: `tests/test_update_review.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class UpdatePolicy: guard_paths: tuple[str, ...]; policy_files: tuple[str, ...]; setup: tuple[str, ...]; canary: tuple[str, ...]; render_config: Callable[[Path], Path]` (guard_paths are `fnmatch` globs, posix-relative); `classify(rows, policy) -> tuple[list[dict], list[dict]]` (guard rows, other rows; a row is guard when its path matches `guard_paths` or equals a `policy_files` entry AND its candidate bytes differ from installed bytes); `render_review(*, bundle, installed_root, source, policy, review_sha256) -> tuple[str, bool]` returns text and `requires_guard_accept`.
- Reads: `<bundle>/complete.json` (`plan.files` rows with `action`, `installed_sha256`, `previous_sha256`, `candidate_sha256`; `merged_settings_hashes`). Read `modding_harness/upgrade.py` near line 488 for the exact row keys before writing code.
- Guard rows render as unified diffs (installed bytes against `<source>/<path>`); binary or non-UTF-8 -> old and new hash; each diff capped at 400 lines with an explicit "N more lines" marker. Other rows render as path, action, short hash. Ends with a `next:` block: what to show the user, the exact `update-apply` command with the hash (and `--accept-guard-changes` only when required), "do not run it until the user says go".

- [ ] **Step 1: Write failing tests:** `test_unchanged_upstream_guard_file_is_not_a_guard_change`; `test_changed_hook_renders_unified_diff_and_requires_accept`; `test_added_and_removed_guard_files_both_listed_and_require_accept`; `test_changed_policy_file_requires_accept`; `test_binary_guard_file_renders_hashes`; `test_diff_cap_names_hidden_line_count`; `test_next_block_has_exact_command_hash_and_flag_only_when_required`; `test_non_guard_changes_alone_do_not_require_accept`.
- [ ] **Step 2: Run** `pytest tests/test_update_review.py -q`; expected FAIL (module missing).
- [ ] **Step 3: Implement** the interface above with `difflib.unified_diff`; decode with `errors="replace"` only after a NUL-byte check.
- [ ] **Step 4: Run** the same command; expected PASS.
- [ ] **Step 5: Commit** `feat: reviewable update with guard files as diffs`.

### Task 2: Stage

**Files:**
- Create: `modding_harness/guarded_update.py` (stage half)
- Test: `tests/test_guarded_update_stage.py`

**Interfaces:**
- Consumes: Task 1; `release.fetch(root, profile, http=...)` (returns `source`, `candidate`, `previous`, `bundle`); `upgrade.stage_review_bundle(root, source, destination, candidate, previous, local_files=..., ...)` as `installation.handle` calls it for `upgrade-stage` (read lines 82-111 of `installation.py`); `release.updates_home(root)`.
- Produces: `class UpdateRefused(ValueError)`; `@dataclass(frozen=True) class Staged: bundle: Path; review_sha256: str; requires_guard_accept: bool; text: str; from_version: str; to_version: str`; `stage_update(root, profile, policy, *, notes_files=(), http=None) -> Staged`. Writes `<work>/meta.json` (`requires_guard_accept`, `review_sha256`, versions).
- Refuses (`UpdateRefused`) when `.modding-agent-harness/update-failed.json` exists.

- [ ] **Step 1: Write failing tests** (fake install, fake release via the `http` seam, as `tests/test_release.py` does): `test_stage_changes_nothing_installed_and_authorizes_nothing`; `test_review_text_and_hash_match_complete_json`; `test_notes_files_and_claude_local_md_untouched`; `test_refuses_while_failed_marker_present`; `test_policy_comes_from_installed_toolkit_not_release` (the release ships a different canary in a policy file; staging used the installed policy and flags the file as a guard change).
- [ ] **Step 2: Run** `pytest tests/test_guarded_update_stage.py -q`; expected FAIL.
- [ ] **Step 3: Implement** `stage_update`: fetch, render-config from `policy.render_config(root)`, stage bundle, render review, write `meta.json`.
- [ ] **Step 4: Run** the same command; expected PASS.
- [ ] **Step 5: Commit** `feat: update staging that authorizes nothing`.

### Task 3: Apply, canary, rollback, fail-closed

**Files:**
- Modify: `modding_harness/guarded_update.py` (apply half), `modding_harness/installation.py` (add `.modding-agent-harness/update-failed.json` to the retained local set the same way PR #13 retained `updates.json`; read its two-line diff first)
- Test: `tests/test_guarded_update_apply.py`

**Interfaces:**
- Consumes: Task 2 `Staged`/`meta.json`; `installation.apply(root, bundle, review_sha256, choices, quiescent=True, notes_files=...)` and `installation.rollback(root, operation, quiescent=True, notes_files=...)`.
- Produces: `@dataclass(frozen=True) class Applied: ok: bool; operation: str; rolled_back: bool; report: str`; `apply_update(root, policy, bundle, review_sha256, choices, *, accept_guard_changes=False, quiescent=False, notes_files=(), run=None) -> Applied`; `clear_failed(root) -> None`; `run(argv, cwd, timeout) -> subprocess.CompletedProcess` seam (default `subprocess.run(..., shell=False, timeout=600, capture_output=True)`).
- Order: refuse without `quiescent`; refuse when `meta.requires_guard_accept` and not `accept_guard_changes`; check every collision and every `merged_settings_hashes` key has a choice; `installation.apply` (its own `upgrade.lock` refusal propagates as `UpdateRefused`); `policy.setup`; `policy.canary`; if the policy files changed also the OLD canary (captured from the installed policy before apply); any setup or canary failure (nonzero, timeout, missing executable) -> `installation.rollback`, then old canary again; second failure -> write the marker (version, operation id, failed step, output tail). Report lists collisions kept, preserved local files, and setup output not restored by rollback.

- [ ] **Step 1: Write failing tests** (tiny real scripts through `sys.executable`): `test_refuses_without_quiescent`; `test_refuses_guard_change_without_accept_flag`; `test_refuses_when_review_sha_differs`; `test_refuses_when_choices_missing_for_a_collision_or_settings_key`; `test_success_runs_setup_then_canary_and_reports`; `test_canary_failure_rolls_back_to_byte_identical_and_reruns_old_canary`; `test_setup_failure_rolls_back_the_same_way`; `test_missing_executable_counts_as_failure`; `test_changed_policy_requires_old_and_new_canary`; `test_double_failure_writes_marker_and_stage_refuses_until_cleared`; `test_marker_survives_a_later_upgrade` (retained file); `test_argv_with_spaces_and_parens_runs_intact`; `test_lock_held_refuses_cleanly`.
- [ ] **Step 2: Run** `pytest tests/test_guarded_update_apply.py -q`; expected FAIL.
- [ ] **Step 3: Implement** per the order above.
- [ ] **Step 4: Run** the same command; expected PASS.
- [ ] **Step 5: Commit** `feat: apply with canary, automatic rollback and fail-closed marker`.

### Task 4: CLI, session-start line, docs, end-to-end

**Files:**
- Modify: `modding_harness/guarded_update.py` (CLI), `modding_harness/release.py` is NOT touched; add the failed-marker line in a new `guarded_update.session_note(root) -> str | None`
- Modify: `README.md` (section "Guarded updates"), `CONTRIBUTING.md` (pointer)
- Create: `tests/test_guarded_update_e2e.py`

**Interfaces:**
- Produces: `COMMANDS = frozenset(("update-stage", "update-apply", "update-clear-failed"))`; `add_arguments(subparsers)`; `handle(args, root, profile, policy, *, notes_files=()) -> dict`. `update-apply` takes `--bundle DIR --review-sha256 SHA --choices FILE [--accept-guard-changes] --quiescent`. `session_note` returns one loud line while the marker exists, else `None`; a toolkit's session-start hook prints it next to `release.note`.

- [ ] **Step 1: Write failing tests:** `test_e2e_stage_then_apply_against_fake_release` (installed bytes equal the release, local notes untouched, `release.check` then reports current); `test_e2e_guard_change_needs_flag_then_succeeds`; `test_e2e_canary_failure_leaves_install_byte_identical`; `test_cli_exit_codes` (refusal 2, partial failure 1, ok 0); `test_session_note_only_while_marker_exists`; `test_new_module_is_vendored` (every new `modding_harness/*.py` is in the vendor script's file set; read `scripts/vendor.py`).
- [ ] **Step 2: Run** `pytest tests/test_guarded_update_e2e.py -q`; expected FAIL.
- [ ] **Step 3: Implement** the CLI and `session_note`; write the README section (the two steps, the second go, failure behaviour, the limits from the spec: GitHub digest is the only verification, the core cannot tell who typed the go, no active-session registry so the caller asserts `--quiescent`) and the CONTRIBUTING pointer.
- [ ] **Step 4: Run** the new test files, then `pytest -q` through the resource wrapper; CI is the full gate.
- [ ] **Step 5: Commit** `feat: guarded update CLI, session note, docs`; open a draft PR based on `feat/self-update`; mark ready only when #13 has merged and CI is green per job; merge only on the user's go.

---

## Follow-on (separate work, not in this plan)

- X4 4.3.1: `ReleaseProfile` + `UpdatePolicy` (guard globs, policy files, setup, canary, render-config), `BUILD-INFO` in the X4 release zip, re-pin the core, wire the commands in `tools/agent-harness.py`, session-start lines, a skill, README and Nexus wording ("first download on Nexus, updates from GitHub through the agent").
- Skyrim: adopt `guarded_update` in place of the prose steps in its `toolkit-update` skill (its session owns this).
