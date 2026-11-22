"""A toolkit names its own private notes files; the core names none of them.

The installer keeps a user's notes out of every upgrade: never overwritten, and never a reason to refuse because the
user edited them while a review was open. Which file names count as notes used to be hardcoded, and one of them was a
single game's. A toolkit now passes its extra names as `notes_files`; `KNOWLEDGEBASE.local.md` stays the shared default.
"""
import hashlib
import re
import tempfile
import unittest
from pathlib import Path

from modding_harness import installation, upgrade

NOTES = "MY-NOTES.md"


def inventory(files):
    return {"v": 1, "files": {path: hashlib.sha256(data).hexdigest() for path, data in files.items()}}


def write(root, files):
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


class NotesFilesTests(unittest.TestCase):
    def stage(self, base, installed, candidate, local_files=()):
        root, source, bundle = base / "installed", base / "source", base / "review"
        root.mkdir()
        source.mkdir()
        write(root, installed)
        write(source, candidate)
        upgrade.stage_review_bundle(root, source, bundle, inventory(candidate), inventory(installed),
                                    local_files=local_files, reserve=0)
        return root, bundle, hashlib.sha256((bundle / "complete.json").read_bytes()).hexdigest()

    def test_notes_edited_after_review_do_not_block_the_upgrade_and_are_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            root, bundle, sha = self.stage(Path(folder), {"owned.txt": b"old"}, {"owned.txt": b"new"}, (NOTES,))
            (root / NOTES).write_bytes(b"typed while the review was open")
            result = installation.apply(root, bundle, sha, quiescent=True, reserve=0, notes_files=(NOTES,))
            self.assertEqual((root / "owned.txt").read_bytes(), b"new")
            self.assertEqual((root / NOTES).read_bytes(), b"typed while the review was open")
            installation.rollback(root, result["operation"], quiescent=True, notes_files=(NOTES,))
            self.assertEqual((root / NOTES).read_bytes(), b"typed while the review was open")

    def test_twin_without_the_name_the_same_edit_is_an_ordinary_changed_file(self):
        """Control: it is the `notes_files` argument, not luck, that lets the edit through."""
        with tempfile.TemporaryDirectory() as folder:
            root, bundle, sha = self.stage(Path(folder), {"owned.txt": b"old"}, {"owned.txt": b"new"}, (NOTES,))
            (root / NOTES).write_bytes(b"typed while the review was open")
            with self.assertRaisesRegex(ValueError, "changed since review: " + NOTES):
                installation.apply(root, bundle, sha, quiescent=True, reserve=0)
            self.assertEqual((root / "owned.txt").read_bytes(), b"old")             # nothing was published

    def test_a_candidate_that_ships_a_notes_name_cannot_overwrite_the_users_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root, bundle, sha = self.stage(Path(folder), {NOTES: b"my own notes"}, {NOTES: b"shipped text"}, (NOTES,))
            with self.assertRaisesRegex(ValueError, "local settings/notes must be retained"):
                installation.apply(root, bundle, sha, {NOTES: "candidate"}, quiescent=True, reserve=0, notes_files=(NOTES,))
            self.assertEqual((root / NOTES).read_bytes(), b"my own notes")

    def test_twin_the_default_list_still_protects_knowledgebase_local(self):
        with tempfile.TemporaryDirectory() as folder:
            name = "KNOWLEDGEBASE.local.md"
            root, bundle, sha = self.stage(Path(folder), {name: b"mine"}, {name: b"shipped"})
            with self.assertRaisesRegex(ValueError, "local settings/notes must be retained"):
                installation.apply(root, bundle, sha, {name: "candidate"}, quiescent=True, reserve=0)
            self.assertEqual((root / name).read_bytes(), b"mine")

    def test_bad_notes_names_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root, bundle, sha = self.stage(Path(folder), {"owned.txt": b"old"}, {"owned.txt": b"new"})
            for bad in (("../escape.md",), ("",), ("/abs.md",), (5,)):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    installation.apply(root, bundle, sha, quiescent=True, reserve=0, notes_files=bad)
            self.assertEqual((root / "owned.txt").read_bytes(), b"old")

    def test_the_shared_cli_forwards_a_toolkits_notes_names_to_stage_apply_and_rollback(self):
        """The path a toolkit really calls: `handle` has to hand the names to all three commands."""
        import argparse
        import json
        parser = argparse.ArgumentParser()
        installation.add_arguments(parser.add_subparsers(dest="command", required=True))
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root, source, bundle = base / "installed", base / "source", base / "review"
            root.mkdir()
            source.mkdir()
            write(root, {"owned.txt": b"old"})
            write(source, {"owned.txt": b"new"})
            candidate = base / "candidate.json"
            candidate.write_text(json.dumps(inventory({"owned.txt": b"new"})))
            previous = base / "previous.json"
            previous.write_text(json.dumps(inventory({"owned.txt": b"old"})))
            staged = installation.handle(parser.parse_args(
                ["upgrade-stage", "--candidate", str(candidate), "--previous", str(previous), "--source", str(source),
                 "--destination", str(bundle), "--installed-root", str(root)]), source, notes_files=(NOTES,))
            self.assertTrue(staged["complete"])
            observed = json.loads((bundle / "complete.json").read_text())["observed_local_files"]
            self.assertIn(NOTES, observed)                          # stage recorded it: it reached upgrade.stage_review_bundle
            (root / NOTES).write_bytes(b"typed while the review was open")
            sha = hashlib.sha256((bundle / "complete.json").read_bytes()).hexdigest()
            applied = installation.handle(parser.parse_args(
                ["upgrade-apply", "--bundle", str(bundle), "--review-sha256", sha, "--installed-root", str(root),
                 "--quiescent"]), source, notes_files=(NOTES,))
            self.assertEqual((root / "owned.txt").read_bytes(), b"new")
            installation.handle(parser.parse_args(
                ["upgrade-rollback", applied["operation"], "--installed-root", str(root), "--quiescent"]),
                source, notes_files=(NOTES,))
            self.assertEqual((root / "owned.txt").read_bytes(), b"old")
            self.assertEqual((root / NOTES).read_bytes(), b"typed while the review was open")

    def test_the_core_source_names_no_games_notes_file(self):
        """A shared package carries no one game's file name. Each toolkit supplies its own through `notes_files`."""
        package = Path(installation.__file__).parent
        hits = [p.name for p in package.glob("*.py") if re.search(r"X4-NOTES", p.read_text(encoding="utf-8"))]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
