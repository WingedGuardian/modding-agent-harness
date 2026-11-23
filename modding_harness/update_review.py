"""What a staged update changes, rendered for the user: guard code as diffs, everything else as a list.

A toolkit supplies an `UpdatePolicy` in code (which paths are guard code, which files are policy, how to run its setup
and canary). `classify` splits the review's file rows into guard changes and the rest. `render_review` reads the staged
bundle's `complete.json` and returns the text to show the user plus whether applying needs the separate guard go
(`--accept-guard-changes`). Nothing here writes anything or runs anything.

A file is a guard change only when its bytes would actually change: a guard path the new release did not touch
(candidate equals the previous release's bytes) is "unchanged upstream", never a guard change by path alone. Files
that are added or removed by the release count as changed. Merged hook settings that differ from the installed
settings count too, because they rewire which hooks run.
"""
from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import upgrade

DIFF_LINES = 400
MAX_REVIEW = 16 * 1024 * 1024
SHORT = 12
_SHA = re.compile(r"[0-9a-f]{64}")
_QUIET = ("unchanged", "absent_retired")


@dataclass(frozen=True)
class UpdatePolicy:
    guard_paths: tuple[str, ...]                # fnmatch globs, posix-relative, matched case-insensitively
    policy_files: tuple[str, ...]               # exact paths of policy files (setup/canary definitions and the like)
    setup: tuple[str, ...]                      # argv of the setup step
    canary: tuple[str, ...]                     # argv of the canary
    render_config: Callable[[Path], Path]       # the toolkit's render configuration for an install root


def _is_guard_path(path, policy):
    folded = path.casefold()
    return (any(fnmatch.fnmatchcase(folded, glob.casefold()) for glob in policy.guard_paths)
            or folded in {name.casefold() for name in policy.policy_files})


def _changes_bytes(row):
    """Would applying the candidate change the file's bytes, and did the release itself change it?"""
    candidate, installed = row.get("candidate_sha256"), row.get("installed_sha256")
    if candidate == installed:
        return False
    # The new release did not touch it: keeping the user's copy is lossless, not a guard change.
    return not (candidate is not None and candidate == row.get("previous_sha256"))


def classify(rows, policy):
    """(guard rows, other rows). A row is a guard change when its path is guard code or a policy file AND the release
    changes its bytes (added, removed or different from the installed copy, and not merely unchanged upstream)."""
    guard, other = [], []
    for row in rows:
        (guard if _is_guard_path(row["path"], policy) and _changes_bytes(row) else other).append(row)
    return guard, other


def _validated_rows(review):
    plan = review.get("plan")
    rows = plan.get("files") if isinstance(plan, dict) else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) and isinstance(row.get("path"), str) for row in rows):
        raise ValueError("review has no plan.files rows")
    upgrade.inventory({"v": 1, "files": dict.fromkeys((row["path"] for row in rows), "0" * 64)})
    for row in rows:
        for key in ("installed_sha256", "previous_sha256", "candidate_sha256"):
            value = row.get(key)
            if value is not None and (not isinstance(value, str) or _SHA.fullmatch(value) is None):
                raise ValueError("review row has an invalid hash: " + row["path"])
        if not isinstance(row.get("action"), str):
            raise ValueError("review row has no action: " + row["path"])
    return rows


def _read(base, name, expected, what):
    """The file's bytes (b'' when it is absent and expected absent), refused unless they match the reviewed hash."""
    path = upgrade._entry(base, name)
    data = path.read_bytes() if path.exists() else None
    if (None if data is None else hashlib.sha256(data).hexdigest()) != expected:
        raise ValueError("{} bytes do not match the reviewed hash: {}".format(what, name))
    return b"" if data is None else data


def _digest(data):
    return hashlib.sha256(data).hexdigest() if data else "(absent)"


def _diff_lines(name, old, new, old_label, new_label):
    """Unified diff lines, or hash lines when the bytes are binary or are not valid UTF-8 text."""
    if b"\0" in old or b"\0" in new:
        return ["binary file, not shown as text:", "  {}: {}".format(old_label, _digest(old)),
                "  {}: {}".format(new_label, _digest(new))]
    hashes = []
    try:
        old.decode("utf-8")
        new.decode("utf-8")
    except UnicodeDecodeError:
        # Replacement characters can make two different byte strings look identical, so the hashes are shown too.
        hashes = ["not valid UTF-8; the diff below uses replacement characters and can hide byte changes:",
                  "  {}: {}".format(old_label, _digest(old)), "  {}: {}".format(new_label, _digest(new))]
    split = lambda data: data.decode("utf-8", errors="replace").splitlines(keepends=True)
    lines = [line.rstrip("\n").replace("\r", "\\r") for line in difflib.unified_diff(
        split(old), split(new), fromfile="{}/{}".format(old_label, name), tofile="{}/{}".format(new_label, name),
        lineterm="")]
    if len(lines) > DIFF_LINES:
        hidden = len(lines) - DIFF_LINES
        lines = lines[:DIFF_LINES] + ["... {} more lines not shown ({} diff lines in all); read the file in full "
                                      "before agreeing".format(hidden, len(lines))]
    return hashes + (lines or ["(no textual difference; bytes differ)"])


def _quote(argument):
    return '"{}"'.format(argument) if not argument or re.search(r"[\s()&^%\"]", argument) else argument


def _next_block(bundle, review_sha256, requires_accept):
    command = "update-apply --bundle {} --review-sha256 {} --choices <file>{} --quiescent".format(
        _quote(str(bundle)), review_sha256, " --accept-guard-changes" if requires_accept else "")
    steps = ["next:", "  1. Show the user this review, with every guard diff above in full."]
    if requires_accept:
        steps.append("  2. The review changes guard code, policy or hook settings: the user must give a separate, "
                     "explicit go for those changes, not only for the update.")
    steps += ["  {}. Write the user's keep/candidate/merged_settings choice for every collision and merged settings "
              "file into the choices file.".format(len(steps)),
              "  {}. Only after the user says go, run exactly:".format(len(steps) + 1),
              "       " + command,
              "     Do not run it until the user says go."]
    return steps


def render_review(*, bundle, installed_root, source, policy, review_sha256):
    """(text, requires_guard_accept) for the staged review in `bundle`; `review_sha256` must be its complete.json's hash."""
    bundle, installed_root, source = Path(bundle).resolve(), Path(installed_root).resolve(), Path(source).resolve()
    if not isinstance(review_sha256, str) or _SHA.fullmatch(review_sha256) is None:
        raise ValueError("review SHA-256 must be 64 lowercase hex digits")
    with upgrade._entry(bundle, "complete.json").open("rb") as stream:
        raw = stream.read(MAX_REVIEW + 1)
    if len(raw) > MAX_REVIEW:
        raise ValueError("upgrade review exceeds parser budget")
    if hashlib.sha256(raw).hexdigest() != review_sha256:
        raise ValueError("review SHA does not match the bundle's complete.json")
    review = json.loads(raw)
    if not isinstance(review, dict) or review.get("v") != 1:
        raise ValueError("unsupported upgrade review")
    rows = _validated_rows(review)
    staged = review.get("candidate_files") if isinstance(review.get("candidate_files"), dict) else {}
    guard, other = classify(rows, policy)

    sections = []
    for row in guard:
        name, candidate = row["path"], row.get("candidate_sha256")
        old = _read(installed_root, name, row.get("installed_sha256"), "installed")
        new = b""
        if candidate is not None:
            # What apply installs is the staged copy in the bundle (rendered for this install); the release's own
            # copy is the fallback for a bundle that holds none.
            copy = upgrade._entry(bundle, "candidate/" + name)
            new = (_read(bundle / "candidate", name, staged.get(name, candidate), "staged candidate") if copy.exists()
                   else _read(source, name, candidate, "release"))
        sections.append(["  {}  [{}]".format(name, row["action"])] +
                        ["    " + line for line in _diff_lines(name, old, new, "installed", "candidate")])

    settings = []
    merged = review.get("merged_settings_hashes")
    for name, digest in sorted((merged if isinstance(merged, dict) else {}).items()):
        if name not in upgrade.PROJECT_SETTINGS or not isinstance(digest, str) or _SHA.fullmatch(digest) is None:
            raise ValueError("review has an unknown or invalid merged settings entry")
        installed_digest = next((row.get("installed_sha256") for row in rows if row["path"] == name), None)
        if installed_digest is None:
            current = upgrade._entry(installed_root, name)
            installed_digest = hashlib.sha256(current.read_bytes()).hexdigest() if current.exists() else None
        if digest == installed_digest:
            continue
        old = _read(installed_root, name, installed_digest, "installed")
        new = _read(bundle, upgrade.PROJECT_SETTINGS[name], digest, "merged settings")
        settings.append(["  {}  [merged hook settings]".format(name)] +
                        ["    " + line for line in _diff_lines(name, old, new, "installed", "merged")])

    requires_accept = bool(sections or settings)
    text = ["Update review (sha256 {})".format(review_sha256), "bundle: {}".format(bundle), ""]
    if requires_accept:
        text.append("GUARD CHANGES ({}): hooks, settings or policy files change. They need a separate go from the "
                    "user.".format(len(sections) + len(settings)))
        for block in sections + settings:
            text += block + [""]
    else:
        text += ["No guard, hook, settings or policy change.", ""]
    quiet = [row for row in other if row["action"] in _QUIET]
    listed = [row for row in other if row["action"] not in _QUIET]
    text.append("Other changes ({}):".format(len(listed)))
    for row in listed:
        short = (row.get("candidate_sha256") or "(none)")[:SHORT]
        note = ""
        if row["action"] == "preserve_collision" and row.get("candidate_sha256") is not None \
                and row.get("candidate_sha256") == row.get("previous_sha256"):
            note = "  unchanged upstream, keep is lossless"
        text.append("  {}  {}  {}{}".format(row["path"], row["action"], short, note))
    if quiet:
        text.append("  {} unchanged or already absent file(s) not listed.".format(len(quiet)))
    text.append("")
    return "\n".join(text + _next_block(bundle, review_sha256, requires_accept)) + "\n", requires_accept
