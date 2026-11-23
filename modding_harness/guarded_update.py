"""Guarded update: stage a release for review, then (later) apply it only on the user's explicit go.

STAGE half: `stage_update` fetches the newest release through `release.fetch`, stages it into a review bundle exactly
the way `installation.handle` does for `upgrade-stage`, renders the review for the user and records what apply must
later check in `meta.json`. It changes nothing installed and authorizes nothing.

APPLY half: `apply_update` applies a staged review only with its SHA-256, `quiescent=True` and, when the review holds
guard changes, `accept_guard_changes=True`. It then runs the toolkit's setup and canary; any failure rolls the
operation back and runs the old canary on the restored state. When that fails too it fails closed: it writes
`update-failed.json`, and staging refuses until the user clears it.

Everything that decides what counts as guard code comes from the INSTALLED toolkit (`ReleaseProfile`,
`UpdatePolicy`), never from the downloaded release.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import installation, release, transport, update_review, upgrade

FAILED_MARKER = ".modding-agent-harness/update-failed.json"
META = "meta.json"
MAX_INVENTORY = 1024 * 1024


class UpdateRefused(ValueError):
    """The update was refused before it changed anything; the message says why."""


@dataclass(frozen=True)
class Staged:
    bundle: Path
    review_sha256: str
    requires_guard_accept: bool
    text: str
    from_version: str
    to_version: str


# --- stage half -------------------------------------------------------------------------------------------------


def _read_json(path):
    """A bounded JSON object, read the way `installation.handle` reads its candidate, previous and render files."""
    with Path(path).open("rb") as stream:
        return transport.load(stream.read(MAX_INVENTORY + 1))


def _discard_earlier_review(work):
    """Remove an earlier review of this same version (our own rebuildable scratch beside the unpacked release), so
    staging again is possible and no meta.json can outlive the bundle it described."""
    for name in ("bundle", META):
        path = work / name
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)


def _write_meta(path, meta):
    data = (json.dumps(meta, sort_keys=True, indent=1) + "\n").encode("utf-8")
    part = path.with_name(".{}.{}.part".format(path.name, uuid.uuid4().hex))
    try:
        with part.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(part, path)
    finally:
        part.unlink(missing_ok=True)


def stage_update(root, profile, policy, *, notes_files=(), http=None, candidate_preparer=None, generated_paths=()):
    """Fetch the newest release and stage it for review. Returns `Staged`; installs and authorizes nothing.

    `candidate_preparer` and `generated_paths` are the toolkit's own renderer hooks, exactly as `installation.handle`
    takes them; with a preparer the render configuration `policy.render_config(root)` is read and handed to it.
    Every refusal is an `UpdateRefused`.
    """
    root = Path(root).resolve()
    if (root / FAILED_MARKER).exists() or (root / FAILED_MARKER).is_symlink():
        raise UpdateRefused("an earlier update failed and left {}; read it, fix the cause and clear it before "
                            "staging another update".format(FAILED_MARKER))
    try:
        notes = installation.notes_set(notes_files)
        fetched = release.fetch(root, profile, **({} if http is None else {"http": http}))
        work = Path(fetched["bundle"]).parent
        _discard_earlier_review(work)
        transform = None
        if candidate_preparer is not None:
            render_path = Path(policy.render_config(root))
            if not render_path.is_file():
                raise UpdateRefused("the toolkit's render configuration is missing: {}".format(render_path))
            config = _read_json(render_path)
            transform = lambda installed, staged, files: candidate_preparer(installed, staged, files, config)
        bundle = Path(fetched["bundle"])
        upgrade.stage_review_bundle(
            root, fetched["source"], bundle, _read_json(fetched["candidate"]),
            _read_json(fetched["previous"]) if fetched["previous"] else None,
            local_files=tuple(sorted(notes)), candidate_transform=transform,
            generated_paths=tuple(generated_paths) if transform is not None else ())
        review_sha256 = hashlib.sha256((bundle / "complete.json").read_bytes()).hexdigest()
        text, requires_accept = update_review.render_review(
            bundle=bundle, installed_root=root, source=fetched["source"], policy=policy, review_sha256=review_sha256)
        staged = Staged(bundle, review_sha256, requires_accept, text, str(fetched["installed"]),
                        str(fetched["version"]))
        _write_meta(bundle.parent / META, {"requires_guard_accept": requires_accept, "review_sha256": review_sha256,
                                           "from_version": staged.from_version, "to_version": staged.to_version})
        return staged
    except UpdateRefused:
        raise
    except ValueError as exc:
        raise UpdateRefused(str(exc)) from exc


# --- apply half -------------------------------------------------------------------------------------------------

TIMEOUT = 600          # seconds per setup or canary run
TAIL_LINES = 20        # lines of each captured stream kept in the report and the marker
SOURCE = "unpacked"    # `release.fetch` unpacks the release beside the bundle under this name
_SHA = re.compile(r"[0-9a-f]{64}")
_OPERATION = re.compile(r"[0-9a-f]{32}")
_JOURNALS = ".modding-agent-harness/upgrades"
_OWN_STATE = frozenset((".modding-agent-harness/upgrade.lock", FAILED_MARKER))   # written by the updater itself


@dataclass(frozen=True)
class Applied:
    ok: bool
    operation: str
    rolled_back: bool
    report: str


def run(argv, cwd, timeout):
    """Run one setup or canary step: an argv list, never a shell, no console window, stdin closed, output captured."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return subprocess.run(list(argv), cwd=str(cwd), shell=False, timeout=timeout, capture_output=True,
                          stdin=subprocess.DEVNULL, creationflags=flags)


_default_run = run


def _refused(message):
    return UpdateRefused(message + "; nothing was changed")


def _argv(argv, what):
    if (not isinstance(argv, (tuple, list)) or not argv
            or any(not isinstance(part, str) or "\0" in part for part in argv) or not argv[0]):
        raise _refused("the toolkit's {} command is not a non-empty argv list of strings".format(what))
    return tuple(argv)


def _tail(data):
    if data is None:
        return []
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    return text.splitlines()[-TAIL_LINES:]


def _step(label, argv, root, runner):
    """Run one step. A nonzero exit, a timeout and an executable that cannot start are all failures, never skips."""
    out = err = None
    try:
        done = runner(argv, root, TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        ok, status, out, err = False, "timed out after {} s".format(exc.timeout), exc.output, exc.stderr
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        ok, status = False, "could not start: {}: {}".format(type(exc).__name__, exc)
    else:
        ok, status, out, err = done.returncode == 0, "exit {}".format(done.returncode), done.stdout, done.stderr
    return {"label": label, "argv": list(argv), "ok": ok, "status": status, "stdout": _tail(out), "stderr": _tail(err)}


def _step_lines(step):
    lines = ["  {}: {} -- {}".format(step["label"], "ok" if step["ok"] else "FAILED", step["status"]),
             "    argv: " + json.dumps(step["argv"])]
    for stream in ("stdout", "stderr"):
        if step[stream]:
            lines.append("    {} (last {} lines at most):".format(stream, TAIL_LINES))
            lines += ["      " + line for line in step[stream]]
    return lines


def _output_tail(step):
    return "\n".join(["stdout:"] + step["stdout"] + ["stderr:"] + step["stderr"]) if step else ""


def _snapshot(root):
    """Size and modification time of every file under the install root except the upgrade journals, links not
    followed: what tells the report which files setup left behind that the rollback journal does not cover. Stat only,
    so a large install costs a directory walk, not a full read; the files rollback restored are verified by its own
    hashes and left out of the comparison (their times change)."""
    found = {}
    for folder, dirs, files in os.walk(root, followlinks=False):
        here = Path(folder)
        relative = here.relative_to(root).as_posix()
        if relative == _JOURNALS:
            dirs[:] = []
            continue
        for name in files:
            path = here / name
            key = path.relative_to(root).as_posix()
            if key in _OWN_STATE:
                continue
            try:
                info = path.lstat()
                found[key] = (info.st_mode, info.st_size, info.st_mtime_ns)
            except OSError as exc:
                found[key] = "unreadable: {}".format(type(exc).__name__)
    return found


def _differences(before, after, verified=()):
    names = sorted((set(before) | set(after)) - set(verified))
    return ["{} ({})".format(name, "added" if name not in before else "removed" if name not in after else "changed")
            for name in names if before.get(name) != after.get(name)]


def _operations(root):
    folder = root / _JOURNALS
    try:
        return {path.name for path in folder.iterdir() if _OPERATION.fullmatch(path.name)} if folder.is_dir() else set()
    except OSError:
        return set()


def _read_review(bundle, review_sha256):
    with upgrade._entry(bundle, "complete.json").open("rb") as stream:
        raw = stream.read(update_review.MAX_REVIEW + 1)
    if len(raw) > update_review.MAX_REVIEW or hashlib.sha256(raw).hexdigest() != review_sha256:
        raise ValueError("review SHA does not match the bundle's complete.json")
    review = json.loads(raw)
    if not isinstance(review, dict) or not isinstance(review.get("plan"), dict):
        raise ValueError("unsupported upgrade review")
    return review


def _write_marker(root, marker):
    """Write the fail-closed marker. Returns None, or the reason it could not be written."""
    try:
        path = root / FAILED_MARKER
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_meta(path, marker)
        return None
    except (OSError, ValueError) as exc:
        return "{}: {}".format(type(exc).__name__, exc)


def clear_failed(root):
    """The user has read the failure and fixed its cause: set the marker aside (renamed, kept as evidence, never
    deleted) so staging works again. Nothing to clear is not an error."""
    path = Path(root).resolve() / FAILED_MARKER
    if not (path.exists() or path.is_symlink()):
        return None
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    os.replace(path, path.with_name("update-failed.cleared-{}-{}.json".format(stamp, uuid.uuid4().hex[:8])))
    return None


def apply_update(root, policy, bundle, review_sha256, choices, *, accept_guard_changes=False, quiescent=False,
                 notes_files=(), run=None, new_policy=None, candidate_validator=None):
    """Apply a staged review, then run setup and canary; roll back on any failure and fail closed if that fails.

    Refusals (nothing changed) raise `UpdateRefused`. Once files may have been published the result is an `Applied`:
    `ok` only when every step passed. The guard decision is re-derived from the bundle's `complete.json` with the
    in-memory `policy`; `meta.json` beside the bundle sits in a user-writable folder, so it is only a cross-check.
    When the review changes the toolkit's policy files, `new_policy` (the candidate's policy, supplied by the toolkit)
    gives the new setup and canary, and the old canary (`policy.canary`) must pass as well.
    `candidate_validator` and `notes_files` are handed to `installation.apply` unchanged.
    """
    runner = _default_run if run is None else run
    root, bundle = Path(root).resolve(), Path(bundle).resolve()
    if quiescent is not True:
        raise _refused("stop host and editor writers first and pass quiescent=True (--quiescent); this flag does not "
                       "stop them")
    if (root / FAILED_MARKER).exists() or (root / FAILED_MARKER).is_symlink():
        raise _refused("an earlier update failed and left {}; read it, fix the cause and clear it first"
                       .format(FAILED_MARKER))
    if not isinstance(review_sha256, str) or _SHA.fullmatch(review_sha256) is None:
        raise _refused("the review SHA-256 must be 64 lowercase hex digits")
    try:
        notes_files = tuple(sorted(installation.notes_set(notes_files)))
        _, requires_accept = update_review.render_review(bundle=bundle, installed_root=root, source=bundle.parent / SOURCE,
                                                         policy=policy, review_sha256=review_sha256)
        review = _read_review(bundle, review_sha256)
    except (OSError, ValueError) as exc:
        raise _refused("the staged review cannot be applied: {}".format(exc)) from exc
    if requires_accept and accept_guard_changes is not True:
        raise _refused("the review changes guard code, policy files or hook settings: applying needs the user's "
                       "separate go for those changes (--accept-guard-changes)")
    try:
        meta = _read_json(bundle.parent / META)
    except (OSError, ValueError) as exc:
        raise _refused("meta.json beside the bundle is missing or unreadable; stage the update again") from exc
    if (meta.get("review_sha256") != review_sha256 or type(meta.get("requires_guard_accept")) is not bool
            or meta["requires_guard_accept"] != requires_accept
            or not all(isinstance(meta.get(key), str) for key in ("from_version", "to_version"))):
        raise _refused("meta.json beside the bundle disagrees with the review it describes; stage the update again")
    from_version, to_version = meta["from_version"], meta["to_version"]

    rows = review["plan"].get("files")
    if not isinstance(rows, list):
        raise _refused("the review has no file rows")
    guard, _ = update_review.classify(rows, policy)
    policy_names = {name.casefold() for name in policy.policy_files}
    policy_changed = sorted(row["path"] for row in guard if row["path"].casefold() in policy_names)
    if policy_changed and new_policy is None:
        raise _refused("the review changes the toolkit's policy files ({}): the toolkit must pass new_policy, the "
                       "candidate's setup and canary, so both old and new canary can be run"
                       .format(", ".join(policy_changed)))
    active = new_policy if policy_changed else policy
    planned = [("setup", _argv(active.setup, "setup")), ("canary", _argv(active.canary, "canary"))]
    old_canary = _argv(policy.canary, "installed canary")
    if policy_changed:
        planned.append(("old canary", old_canary))

    if not isinstance(choices, dict):
        raise _refused("choices must map each collision and merged settings file to keep, candidate or merged_settings")
    paths = {row["path"] for row in rows}
    merged = review.get("merged_settings_hashes")
    needed = {row["path"] for row in rows if row.get("action") == "preserve_collision"}
    # A merged settings file that is not a review row cannot take a choice (apply would refuse it as unknown).
    needed |= {name for name in (merged if isinstance(merged, dict) else {}) if name in paths}
    missing = sorted(needed - set(choices))
    if missing:
        raise _refused("every collision and merged settings file needs the user's explicit choice; missing: "
                       + ", ".join(missing))

    earlier = _operations(root)
    before = _snapshot(root)
    try:
        result = installation.apply(root, bundle, review_sha256, choices, quiescent=True,
                                    candidate_validator=candidate_validator, notes_files=notes_files)
    except Exception as exc:
        started = sorted(_operations(root) - earlier)
        published = bool(started) and any((root / _JOURNALS / started[0]).glob("*-intent.json"))
        if len(started) == 1 and published:
            # Files may already be replaced: recover exactly as after a failed step.
            failed = {"label": "apply", "argv": [], "ok": False, "status": "failed: {}".format(exc),
                      "stdout": [], "stderr": []}
            return _recover(root, old_canary, runner, notes_files, started[0], [failed], failed, before,
                            review, choices, from_version, to_version)
        if isinstance(exc, TimeoutError):
            raise _refused("the upgrade lock .modding-agent-harness/upgrade.lock is held by another updater, or apply "
                           "ran out of time before changing anything: {}".format(exc)) from exc
        if isinstance(exc, (OSError, ValueError)):
            raise _refused("apply refused: {}".format(exc)) from exc
        raise
    operation = result["operation"]
    steps = []
    for label, argv in planned:
        step = _step(label, argv, root, runner)
        steps.append(step)
        if not step["ok"]:
            return _recover(root, old_canary, runner, notes_files, operation, steps, step, before, review,
                            choices, from_version, to_version)
    lines = ["Update {} -> {}: APPLIED, setup and canary passed.".format(from_version, to_version),
             "operation: {} (undo with upgrade-rollback {} --quiescent)".format(operation, operation)]
    lines += _common(review, choices, steps, result.get("published", []))
    lines += ["Not restored by rollback:",
              "  whatever setup generated or deployed is outside the rollback journal; a later upgrade-rollback of this",
              "  operation restores the installed files only.", ""]
    return Applied(True, operation, False, "\n".join(lines) + "\n")


def _common(review, choices, steps, published):
    rows = review["plan"].get("files", [])
    kept = sorted(row["path"] for row in rows if row.get("action") == "preserve_collision"
                  and choices.get(row["path"]) == "keep")
    local = review.get("observed_local_files") if isinstance(review.get("observed_local_files"), dict) else {}
    preserved = sorted({name for name, digest in local.items() if digest is not None} |
                       {row["path"] for row in rows if row.get("action") == "preserve_local"})
    lines = ["This apply assumed it was the only writer (quiescent). A running agent may keep the hooks it loaded",
             "before the update until it restarts.", "", "Files published ({}):".format(len(published))]
    lines += ["  " + name for name in published] + ["", "Steps:"]
    for step in steps:
        lines += _step_lines(step)
    lines += ["", "Collisions kept ({}):".format(len(kept))] + ["  " + name for name in kept]
    lines += ["", "Local files preserved ({}):".format(len(preserved))] + ["  " + name for name in preserved] + [""]
    return lines


def _recover(root, old_canary, runner, notes_files, operation, steps, failed, before, review, choices,
             from_version, to_version):
    """A step failed after files were (or may have been) published: roll back, then the old canary on the restored
    state. Either of those failing writes the fail-closed marker."""
    marker = {"v": 1, "from_version": from_version, "to_version": to_version, "operation": operation,
              "first_failure": failed["label"], "first_failure_status": failed["status"],
              "written": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    rolled_back, problem = False, None
    try:
        restored = installation.rollback(root, operation, quiescent=True, notes_files=notes_files)
        rolled_back = True
    except Exception as exc:
        restored = None
        marker.update(failed_step="rollback", error="{}: {}".format(type(exc).__name__, exc),
                      output_tail=_output_tail(failed))
        problem = "the rollback failed: {}: {}".format(type(exc).__name__, exc)
    if rolled_back:
        check = _step("canary after rollback", old_canary, root, runner)
        steps = [*steps, check]
        if not check["ok"]:
            marker.update(failed_step="canary after rollback", output_tail=_output_tail(check))
            problem = "the installed canary fails on the restored state"
    head = ("Update {} -> {}: FAILED at {} ({}); {}.".format(
        from_version, to_version, failed["label"], failed["status"],
        "rolled back to the previous install" if rolled_back else "NOT rolled back"))
    lines = [head, "operation: {}".format(operation)]
    if restored is not None:
        lines.append("rollback restored {} file(s), set {} new file(s) aside in {}".format(
            len(restored.get("restored", [])), len(restored.get("retained_new_files", [])), restored.get("journal")))
    if problem is not None:
        reason = _write_marker(root, marker)
        lines += ["", "FAIL CLOSED: {}. The guards may be broken or missing.".format(problem)]
        lines.append("Wrote {}; staging refuses until the user reads it, fixes the cause and clears it.".format(
            FAILED_MARKER) if reason is None else
            "COULD NOT WRITE {} ({}): tell the user now; do not trust the guards.".format(FAILED_MARKER, reason))
    lines.append("")
    lines += _common(review, choices, steps, [])
    lines += ["Not restored by rollback:"]
    if rolled_back:
        verified = [*restored.get("restored", []), *restored.get("retained_new_files", [])]
        lines += (["  " + name for name in _differences(before, _snapshot(root), verified)]
                  or ["  (no file under the install root)"])
    else:
        lines.append("  unknown: the rollback did not complete; journal {}".format(
            root / _JOURNALS / operation))
    lines += ["  setup's effects outside the install root (deployed copies, for example) are not tracked here.", ""]
    return Applied(False, operation, rolled_back, "\n".join(lines) + "\n")
