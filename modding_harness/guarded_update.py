"""Guarded update: stage a release for review, then (later) apply it only on the user's explicit go.

This is the STAGE half. `stage_update` fetches the newest release through `release.fetch`, stages it into a review
bundle exactly the way `installation.handle` does for `upgrade-stage`, renders the review for the user and records
what apply must later check in `meta.json`. It changes nothing installed and authorizes nothing: applying is a
separate step that needs the review's SHA-256 and, when the review holds guard changes, a separate guard go.

Everything that decides what counts as guard code comes from the INSTALLED toolkit (`ReleaseProfile`,
`UpdatePolicy`), never from the downloaded release.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
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
