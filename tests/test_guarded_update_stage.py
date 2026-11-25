"""Guarded update, stage half: a staged update shows the review and authorizes nothing.

The fake install and fake release go through the same `http` seam `tests/test_release.py` uses; `urlopen` is replaced
so a test that forgot the fake fails instead of reaching GitHub. Nothing installed may change while staging.
"""
import hashlib
import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from modding_harness import guarded_update, release, update_review
from modding_harness.update_review import UpdatePolicy
from test_release import REPO, FakeGitHub, _no_network, make_zip, profile

HOOK = ".claude/hooks/guard.py"
POLICY_FILE = "tools/update_policy.json"
FAILED = ".modding-agent-harness/update-failed.json"
NEW = "toolkit-4.3.0/"
OLD = "toolkit-4.2.0/"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def make_policy(config=None):
    return UpdatePolicy(guard_paths=(".claude/hooks/*",), policy_files=(POLICY_FILE,), setup=("python", "setup.py"),
                        canary=("python", "canary.py"), render_config=lambda root: config or root / "render.json")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "install"
        self.root.mkdir()
        self.files = {"setup.sh": b"old\n", HOOK: b"guard v1\n", POLICY_FILE: b'{"canary": "A"}\n',
                      "CLAUDE.local.md": b"my private rules\n", "KNOWLEDGEBASE.local.md": b"my private notes\n",
                      "NOTES.md": b"toolkit notes the user keeps\n"}
        for name, data in self.files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for patcher in (mock.patch.object(urllib.request, "urlopen", _no_network),
                        mock.patch.dict(os.environ, {"MODDING_HARNESS_UPDATES_DIR": str(Path(self.tmp.name) / "updates")})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def github(self, extra=None, previous_extra=None, **kwargs):
        """A fake GitHub whose 4.2.0 zip holds what is installed and whose 4.3.0 zip, by default, changes no guard file."""
        previous = make_zip("4.2.0", commit="c" * 40, extra=previous_extra if previous_extra is not None else
                            {OLD + HOOK: self.files[HOOK], OLD + POLICY_FILE: self.files[POLICY_FILE]})
        shipped = {NEW + HOOK: self.files[HOOK], NEW + POLICY_FILE: self.files[POLICY_FILE]}   # unchanged by default
        return FakeGitHub(body=make_zip(extra={**shipped, **(extra or {})}), previous=previous, **kwargs)

    def stage(self, gh=None, policy=None, **kwargs):
        return guarded_update.stage_update(self.root, profile(), policy or make_policy(),
                                           http=gh or self.github(), **kwargs)


class StageTests(Base):
    def test_stage_changes_nothing_installed_and_authorizes_nothing(self):
        before = self.snapshot()
        staged = self.stage(self.github(extra={NEW + HOOK: b"guard v2\n"}))
        self.assertEqual(self.snapshot(), before)                                   # every installed byte, nothing added
        self.assertFalse((self.root / ".modding-agent-harness/upgrades").exists())   # no operation journal: not applied
        self.assertTrue(staged.requires_guard_accept)
        self.assertRegex(Path(staged.bundle).name, r"^bundle(-[0-9a-f]{12})?$")
        self.assertNotIn(self.root.resolve(), Path(staged.bundle).resolve().parents)
        complete = json.loads((staged.bundle / "complete.json").read_text())
        self.assertIn("install unchanged", complete["mode"])
        self.assertIn("Do not run it until the user says go.", staged.text)
        self.assertIn("--accept-guard-changes", staged.text)                        # asked for, never granted
        self.assertEqual((staged.from_version, staged.to_version), ("4.2.0", "4.3.0"))

    def test_review_text_and_hash_match_complete_json(self):
        staged = self.stage(self.github(extra={NEW + HOOK: b"guard v2\n"}))
        raw = (staged.bundle / "complete.json").read_bytes()
        self.assertEqual(staged.review_sha256, sha(raw))
        self.assertIn(staged.review_sha256, staged.text)
        self.assertIn("-guard v1", staged.text)                                     # the guard diff is shown in full
        self.assertIn("+guard v2", staged.text)
        meta = json.loads((staged.bundle.parent / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta, {"requires_guard_accept": True, "review_sha256": staged.review_sha256,
                                "from_version": "4.2.0", "to_version": "4.3.0"})
        again, flag = update_review.render_review(bundle=staged.bundle, installed_root=self.root,
                                                  source=staged.bundle.parent / "unpacked", policy=make_policy(),
                                                  review_sha256=staged.review_sha256)
        self.assertEqual((again, flag), (staged.text, staged.requires_guard_accept))

    def test_no_guard_change_is_recorded_as_not_requiring_the_guard_go(self):
        staged = self.stage()
        self.assertFalse(staged.requires_guard_accept)
        self.assertIn("No guard, hook, settings or policy change.", staged.text)
        meta = json.loads((staged.bundle.parent / "meta.json").read_text(encoding="utf-8"))
        self.assertFalse(meta["requires_guard_accept"])

    def test_notes_files_and_claude_local_md_untouched(self):
        # The release ships the built-in local notes names. The user's copies must stay, be held as rollback evidence
        # and be observed by the review (apply refuses if they change meanwhile). NOTES.md is the toolkit's own extra
        # name, so only `notes_files` can make the review observe it.
        extra = {NEW + "CLAUDE.local.md": b"shipped\n", NEW + "KNOWLEDGEBASE.local.md": b"shipped\n",
                 NEW + "NOTES.md": b"shipped notes\n"}
        before = self.snapshot()
        staged = self.stage(self.github(extra=extra), notes_files=("NOTES.md",))
        self.assertEqual(self.snapshot(), before)
        observed = json.loads((staged.bundle / "complete.json").read_text())["observed_local_files"]
        for name in ("CLAUDE.local.md", "KNOWLEDGEBASE.local.md", "NOTES.md"):
            with self.subTest(name):
                self.assertEqual(observed.get(name), sha(self.files[name]))
                self.assertEqual((self.root / name).read_bytes(), self.files[name])
                self.assertEqual((staged.bundle / "rollback" / name).read_bytes(), self.files[name])

    def test_refuses_while_failed_marker_present(self):
        marker = self.root / FAILED
        marker.parent.mkdir(parents=True)
        marker.write_text('{"v": 1, "version": "4.3.0"}')
        gh = self.github()
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "update-failed"):
            self.stage(gh)
        self.assertEqual(gh.calls, [])                                              # refused before any network use
        self.assertFalse(release.updates_home(self.root).exists())

    def test_policy_comes_from_installed_toolkit_not_release(self):
        # The release ships a different canary in the policy file the INSTALLED toolkit names, and a policy file
        # of its own naming that the installed toolkit never declared.
        extra = {NEW + POLICY_FILE: b'{"canary": "B"}\n', NEW + "tools/release_declared_policy.json": b"{}\n",
                 NEW + "UpdatePolicy.json": b'{"guard_paths": ["*"]}\n'}
        gh = self.github(extra=extra)
        staged = self.stage(gh)
        self.assertTrue(staged.requires_guard_accept)
        guard_section = staged.text.split("Other changes", 1)[0]
        self.assertIn(POLICY_FILE, guard_section)
        self.assertIn('+{"canary": "B"}', guard_section)
        self.assertNotIn("release_declared_policy.json", guard_section)             # not a guard change by its own say-so
        self.assertNotIn("UpdatePolicy.json", guard_section)
        self.assertTrue(all(call.startswith("https://api.github.com/repos/{}/".format(REPO)) or
                            call.startswith("https://github.com/{}/releases/download/".format(REPO))
                            for call in gh.calls))

    def test_a_refusal_from_the_release_is_an_update_refused(self):
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "no newer release"):
            guarded_update.stage_update(self.root, profile("4.3.0"), make_policy(), http=self.github())
        self.assertTrue(issubclass(guarded_update.UpdateRefused, ValueError))

    def test_staging_again_replaces_the_earlier_review_and_its_meta(self):
        first = self.stage()
        second = self.stage(self.github(extra={NEW + HOOK: b"guard v2\n"}))
        self.assertNotEqual(first.bundle, second.bundle)                          # a fresh bundle folder per fetch
        self.assertFalse(first.bundle.exists())                                     # the earlier review is gone
        self.assertTrue(second.bundle.is_dir())
        self.assertNotEqual(first.review_sha256, second.review_sha256)
        meta = json.loads((second.bundle.parent / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["review_sha256"], second.review_sha256)
        self.assertEqual(meta["review_sha256"], sha((second.bundle / "complete.json").read_bytes()))
        self.assertNotEqual(meta["review_sha256"], first.review_sha256)             # meta no longer describes the first

    def test_staging_removes_only_earlier_bundle_folders_beside_the_work_folder(self):
        first = self.stage()
        work = first.bundle.parent
        stale = [work / "bundle", work / "bundle-0123456789ab", work / "bundle-fedcba987654"]
        for folder in stale:
            (folder / "rollback").mkdir(parents=True)
            (folder / "complete.json").write_text("{}", encoding="utf-8")
        keep = [work / "bundle-FEDCBA012345", work / "bundle-0123456789a", work / "bundle-0123456789abc",
                work / "bundle.old", work / "bundles", work / "mybundle-0123456789ab"]
        for folder in keep:
            folder.mkdir()
        (work / "unpacked-extra").mkdir()
        candidate = work / "candidate.json"
        candidate_before = candidate.read_bytes() if candidate.exists() else None
        if candidate_before is None:
            candidate.write_bytes(b"{}")
            candidate_before = b"{}"
        second = self.stage()
        self.assertEqual(second.bundle.parent, work)
        for folder in stale + [first.bundle]:
            self.assertFalse(folder.exists(), folder.name)
        for folder in keep + [work / "unpacked", work / "unpacked-extra"]:
            self.assertTrue(folder.is_dir(), folder.name)
        self.assertEqual(candidate.read_bytes(), candidate_before)
        self.assertTrue(second.bundle.is_dir())

    def test_a_failed_stage_leaves_no_meta_behind(self):
        first = self.stage()
        meta = first.bundle.parent / "meta.json"
        self.assertTrue(meta.is_file())
        with mock.patch.object(update_review, "render_review", side_effect=ValueError("review is unreadable")):
            with self.assertRaisesRegex(guarded_update.UpdateRefused, "unreadable"):
                self.stage()
        self.assertFalse(meta.exists())                 # never a meta that does not describe the bundle beside it

    def test_render_config_comes_from_the_policy_and_rendering_goes_through_the_preparer(self):
        config = Path(self.tmp.name) / "render.json"
        config.write_text('{"v": 1, "root": "destination"}')
        seen = []

        def prepare(installed, staged, files, data):
            seen.append((Path(installed), Path(staged), dict(data)))
            return {"setup.sh": b"rendered for this install\n"}
        staged = self.stage(policy=make_policy(config), candidate_preparer=prepare)
        self.assertEqual(seen[0][0], self.root.resolve())
        self.assertEqual(seen[0][2], {"v": 1, "root": "destination"})
        self.assertEqual((staged.bundle / "candidate/setup.sh").read_bytes(), b"rendered for this install\n")
        self.assertEqual((self.root / "setup.sh").read_bytes(), b"old\n")

    def test_a_missing_render_config_refuses_when_a_preparer_needs_it(self):
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "render"):
            self.stage(policy=make_policy(Path(self.tmp.name) / "absent.json"), candidate_preparer=lambda *a: {})


if __name__ == "__main__":
    unittest.main()
