import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from modding_harness import durable, installation, preferences, upgrade


def inventory(files):
    return {"v": 1, "files": {path: hashlib.sha256(data).hexdigest() for path, data in files.items()}}


def write(root, files):
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


class InstallationTests(unittest.TestCase):
    def prepare(self, base, old=None, new=None, previous=None, source_mode=None):
        root, source, bundle = base / "installed", base / "source", base / "review"
        root.mkdir()
        source.mkdir()
        old = old or {"owned.txt": b"old"}
        new = new or {"owned.txt": b"new", "z-new.txt": b"created"}
        write(root, old)
        write(source, new)
        if source_mode is not None:
            for name in new:
                (source / name).chmod(source_mode)
        upgrade.stage_review_bundle(root, source, bundle, inventory(new), inventory(previous or old), reserve=0)
        return root, source, bundle, hashlib.sha256((bundle / "complete.json").read_bytes()).hexdigest()

    def test_upgrade_and_rollback_retain_all_evidence_and_private_notes(self):
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, sha = self.prepare(Path(folder))
            (root / "KNOWLEDGEBASE.local.md").write_bytes(b"personal notes after review")
            result = installation.apply(root, bundle, sha, quiescent=True, reserve=0)
            self.assertEqual((root / "owned.txt").read_bytes(), b"new")
            self.assertEqual((Path(result["journal"]) / "rollback/owned.txt").read_bytes(), b"old")
            self.assertTrue(result["host_review_required"])
            rolled = installation.rollback(root, result["operation"], quiescent=True)
            self.assertEqual((root / "owned.txt").read_bytes(), b"old")
            self.assertFalse((root / "z-new.txt").exists())
            self.assertEqual((Path(rolled["journal"]) / "retained/z-new.txt").read_bytes(), b"created")
            self.assertEqual((root / "KNOWLEDGEBASE.local.md").read_bytes(), b"personal notes after review")
            self.assertEqual(installation.rollback(root, result["operation"], quiescent=True)["restored"], [])

    def test_quiescence_review_hash_and_current_bytes_are_required_before_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, sha = self.prepare(Path(folder))
            for digest, quiescent in ((sha, False), ('0' * 64, True)):
                with self.assertRaises(ValueError):
                    installation.apply(root, bundle, digest, quiescent=quiescent, reserve=0)
            (root / "owned.txt").write_bytes(b"user changed after review")
            with self.assertRaisesRegex(ValueError, "installed bytes changed"):
                installation.apply(root, bundle, sha, quiescent=True, reserve=0)
            self.assertFalse((root / ".modding-agent-harness").exists())

    def test_read_only_target_refuses_before_any_publication_or_permission_change(self):
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, sha = self.prepare(Path(folder),
                old={"z-protected.txt": b"old"}, new={"a-new.txt": b"created", "z-protected.txt": b"new"})
            target = root / "z-protected.txt"
            target.chmod(0o444)
            protected_mode = target.stat().st_mode
            try:
                with self.assertRaisesRegex(ValueError, "read-only installed target"):
                    installation.apply(root, bundle, sha, quiescent=True, reserve=0)
                self.assertEqual(target.read_bytes(), b"old")
                self.assertEqual(target.stat().st_mode, protected_mode)
                self.assertFalse((root / "a-new.txt").exists())
                self.assertFalse((root / ".modding-agent-harness").exists())
            finally:
                target.chmod(0o644)  # only this test's owned temporary file

    def test_selected_readonly_source_mode_refuses_before_any_installed_change(self):
        for mode in (0o444, 0o555):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                root, source, bundle, sha = self.prepare(Path(folder), source_mode=mode)
                before_mode = (root / "owned.txt").stat().st_mode
                with self.assertRaisesRegex(ValueError, "read-only candidate mode"):
                    installation.apply(root, bundle, sha, quiescent=True, reserve=0)
                self.assertEqual((root / "owned.txt").read_bytes(), b"old")
                self.assertEqual((root / "owned.txt").stat().st_mode, before_mode)
                self.assertFalse((root / "z-new.txt").exists())
                self.assertFalse((root / ".modding-agent-harness").exists())

    def test_unselected_readonly_candidate_does_not_block_keeping_installed_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, sha = self.prepare(Path(folder), new={"owned.txt": b"new"}, source_mode=0o444)
            result = installation.apply(root, bundle, sha, {"owned.txt": "keep"}, quiescent=True, reserve=0)
            self.assertEqual(result["published"], [])
            self.assertEqual((root / "owned.txt").read_bytes(), b"old")

    def test_candidate_and_rollback_corruption_refuse_before_target_changes(self):
        for relative in ("candidate/owned.txt", "rollback/owned.txt"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as folder:
                root, source, bundle, sha = self.prepare(Path(folder))
                (bundle / relative).write_bytes(b"corrupt")
                with self.assertRaises(ValueError):
                    installation.apply(root, bundle, sha, quiescent=True, reserve=0)
                self.assertEqual((root / "owned.txt").read_bytes(), b"old")

    def test_read_only_published_target_refuses_whole_rollback_without_lifting_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, sha = self.prepare(Path(folder))
            result = installation.apply(root, bundle, sha, quiescent=True, reserve=0)
            target = root / "owned.txt"
            target.chmod(0o444)
            protected_mode = target.stat().st_mode
            try:
                with self.assertRaisesRegex(ValueError, "read-only installed target"):
                    installation.rollback(root, result["operation"], quiescent=True)
                self.assertEqual(target.read_bytes(), b"new")
                self.assertEqual(target.stat().st_mode, protected_mode)
                self.assertEqual((root / "z-new.txt").read_bytes(), b"created")
                self.assertFalse((Path(result["journal"]) / "rollbacks").exists())
            finally:
                target.chmod(0o644)  # only this test's owned temporary file

    def test_a_local_policy_appearing_after_review_refuses_before_any_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, sha = self.prepare(Path(folder))
            local = root / ".claude/settings.local.json"
            local.parent.mkdir()
            local.write_bytes(b'{"hooks":{"PreToolUse":[{"hooks":[{"type":"command","command":"new personal policy"}]}]}}')
            with self.assertRaisesRegex(ValueError, "local configuration or policy source changed"):
                installation.apply(root, bundle, sha, quiescent=True, reserve=0)
            self.assertEqual((root / "owned.txt").read_bytes(), b"old")

    def test_local_policy_changed_during_staging_refuses_before_permission(self):
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, sha = self.prepare(Path(folder))
            real = durable.copy_verified
            def race(src, dst, deadline=None):
                result = real(src, dst, deadline)
                if Path(src) == bundle / "candidate/owned.txt":
                    path = root / '.claude/settings.local.json'
                    path.parent.mkdir()
                    path.write_bytes(b'{"hooks":{"PreToolUse":[]}}')
                return result
            with mock.patch.object(durable, 'copy_verified', side_effect=race):
                with self.assertRaisesRegex(installation.UpgradeFailed, "published=\\[\\]"):
                    installation.apply(root, bundle, sha, quiescent=True, reserve=0)
            self.assertEqual((root / 'owned.txt').read_bytes(), b'old')

    def test_collisions_require_explicit_resolution_and_can_retain_personal_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, sha = self.prepare(Path(folder), old={"notes.md": b"personal"},
                new={"notes.md": b"shipped", "new.txt": b"new"}, previous={"notes.md": b"previous default"})
            with self.assertRaisesRegex(ValueError, "unresolved installed collision"):
                installation.apply(root, bundle, sha, quiescent=True, reserve=0)
            installation.apply(root, bundle, sha, {"notes.md": "keep"}, quiescent=True, reserve=0)
            self.assertEqual((root / "notes.md").read_bytes(), b"personal")

    def test_merged_settings_hash_is_bound_and_custom_local_pre_policy_blocks_application(self):
        old = {".claude/settings.json": b'{"permissions":{"deny":["personal"]}}'}
        new = {".claude/settings.json": b'{"hooks":{"PostToolUse":[]}}'}
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, sha = self.prepare(Path(folder), old, new)
            (bundle / "merged-settings.json").write_bytes(b'{"permissions":{}}')
            with self.assertRaisesRegex(ValueError, "staged candidate bytes changed"):
                installation.apply(root, bundle, sha, {".claude/settings.json": "merged_settings"}, quiescent=True, reserve=0)
            self.assertEqual((root / ".claude/settings.json").read_bytes(), old[".claude/settings.json"])
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            local = b'{"hooks":{"PreToolUse":[{"hooks":[{"type":"command","command":"personal guard"}]}]}}'
            root, source, bundle, sha = self.prepare(base, {**old, ".claude/settings.local.json": local}, new)
            with self.assertRaisesRegex(ValueError, "ordered-policy integration"):
                installation.apply(root, bundle, sha, {".claude/settings.json": "keep"}, quiescent=True, reserve=0)

    def test_publication_progress_failure_is_recoverable_from_owned_intent(self):
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, sha = self.prepare(Path(folder), new={"owned.txt": b"new"})
            real = durable.atomic_json
            def fail_progress(path, data):
                if str(path).endswith('-published.json'):
                    raise OSError("progress storage failed")
                return real(path, data)
            with mock.patch.object(durable, "atomic_json", side_effect=fail_progress):
                with self.assertRaises(installation.UpgradeFailed):
                    installation.apply(root, bundle, sha, quiescent=True, reserve=0)
            self.assertEqual((root / "owned.txt").read_bytes(), b"new")
            operation, = (root / ".modding-agent-harness/upgrades").iterdir()
            installation.rollback(root, operation.name, quiescent=True)
            self.assertEqual((root / "owned.txt").read_bytes(), b"old")

    def test_concurrent_new_destination_is_preserved_and_partial_upgrade_is_named(self):
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, sha = self.prepare(Path(folder))
            real = durable.atomic_json
            def race(path, data):
                result = real(path, data)
                # A foreign creator arrives after path checks, independently
                # of whichever OS publication primitive the upgrader uses.
                if str(path).endswith('-intent.json') and data.get('path') == 'z-new.txt':
                    (root / 'z-new.txt').write_bytes(b'foreign')
                return result
            with mock.patch.object(durable, "atomic_json", side_effect=race):
                with self.assertRaisesRegex(installation.UpgradeFailed, "published=.*owned.txt"):
                    installation.apply(root, bundle, sha, quiescent=True, reserve=0)
            self.assertEqual((root / "z-new.txt").read_bytes(), b"foreign")
            operation, = (root / ".modding-agent-harness/upgrades").iterdir()
            with self.assertRaisesRegex(ValueError, "foreign or changed"):
                installation.rollback(root, operation.name, quiescent=True)
            self.assertEqual((root / "z-new.txt").read_bytes(), b"foreign")

    def test_foreign_rename_with_identical_bytes_is_not_owned_by_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, sha = self.prepare(Path(folder), new={"owned.txt": b"new"})
            result = installation.apply(root, bundle, sha, quiescent=True, reserve=0)
            foreign = root / "foreign"
            foreign.write_bytes(b"new")
            os.replace(foreign, root / "owned.txt")
            with self.assertRaisesRegex(ValueError, "foreign or changed"):
                installation.rollback(root, result["operation"], quiescent=True)

    def test_knowledgebase_update_preserves_unclassified_notes_and_rollback_keeps_them(self):
        with tempfile.TemporaryDirectory() as folder:
            old = b'# Local legacy notes\n## My preferences\nUnclassified legacy prose\n'
            root, source, bundle, sha = self.prepare(Path(folder), {"KNOWLEDGEBASE.md": old}, {"KNOWLEDGEBASE.md": b"reference"})
            result = installation.apply(root, bundle, sha, quiescent=True, reserve=0)
            self.assertEqual(preferences.render(root), '')
            retained, = root.glob('KNOWLEDGEBASE.pre-4.0-*.md')
            self.assertEqual(retained.read_bytes(), old)
            installation.rollback(root, result["operation"], quiescent=True)
            self.assertEqual((root / "KNOWLEDGEBASE.md").read_bytes(), old)
            self.assertTrue((root / "KNOWLEDGEBASE.local.md").exists())

    def test_recovery_stores_and_local_settings_cannot_be_overwritten(self):
        for name in ('.modding-agent-harness/backups/saves/blob', '.claude/settings.local.json'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root, source, bundle, sha = self.prepare(Path(folder), new={name: b"unsafe"})
                with self.assertRaises(ValueError):
                    installation.apply(root, bundle, sha, quiescent=True, reserve=0)
                self.assertFalse((root / name).exists())

    def test_low_space_and_expired_deadline_never_publish_a_toolkit_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, sha = self.prepare(Path(folder))
            usage = installation.shutil.disk_usage(root)
            with mock.patch.object(installation.shutil, 'disk_usage', return_value=usage._replace(free=0)):
                with self.assertRaises(OSError):
                    installation.apply(root, bundle, sha, quiescent=True, reserve=1)
            with self.assertRaises(TimeoutError):
                installation.apply(root, bundle, sha, quiescent=True, reserve=0, deadline=0)
            self.assertEqual((root / 'owned.txt').read_bytes(), b'old')
            self.assertFalse((root / 'z-new.txt').exists())

    def test_corrupt_permanent_rollback_refuses_the_entire_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, sha = self.prepare(Path(folder))
            result = installation.apply(root, bundle, sha, quiescent=True, reserve=0)
            (Path(result['journal']) / 'rollback/owned.txt').write_bytes(b'rot')
            with self.assertRaisesRegex(ValueError, 'rollback evidence is damaged'):
                installation.rollback(root, result['operation'], quiescent=True)
            self.assertEqual((root / 'owned.txt').read_bytes(), b'new')
            self.assertEqual((root / 'z-new.txt').read_bytes(), b'created')

    def test_explicit_merged_settings_preserve_permissions_and_rollback_original_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            old = b'{"permissions":{"deny":["personal"]},"model":"user"}'
            new = b'{"hooks":{"PreToolUse":[{"hooks":[{"type":"command","command":"toolkit guard"}]}]}}'
            name = '.claude/settings.json'
            root, source, bundle, sha = self.prepare(Path(folder), {name: old}, {name: new})
            result = installation.apply(root, bundle, sha, {name: 'merged_settings'}, quiescent=True, reserve=0)
            merged = json.loads((root / name).read_bytes())
            self.assertEqual(merged['permissions']['deny'], ['personal'])
            self.assertEqual(merged['model'], 'user')
            self.assertEqual(merged['hooks']['PreToolUse'][0]['hooks'][0]['command'], 'toolkit guard')
            installation.rollback(root, result['operation'], quiescent=True)
            self.assertEqual((root / name).read_bytes(), old)

    def test_candidate_file_mode_is_bound_to_the_review(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root, source, bundle = base / 'installed', base / 'source', base / 'review'
            root.mkdir()
            source.mkdir()
            write(source, {'run.sh': b'#!/bin/sh\nexit 0\n'})
            (source / 'run.sh').chmod(0o755)
            observed_mode = (source / 'run.sh').stat().st_mode & 0o777
            upgrade.stage_review_bundle(root, source, bundle, inventory({'run.sh': b'#!/bin/sh\nexit 0\n'}), reserve=0)
            sha = hashlib.sha256((bundle / 'complete.json').read_bytes()).hexdigest()
            installation.apply(root, bundle, sha, quiescent=True, reserve=0)
            self.assertEqual((root / 'run.sh').stat().st_mode & 0o777, observed_mode)

    def test_shared_cli_applies_rolls_back_and_retains_partial_failure_details(self):
        import argparse
        parser = argparse.ArgumentParser()
        installation.add_arguments(parser.add_subparsers(dest='command', required=True))
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, sha = self.prepare(Path(folder))
            args = parser.parse_args(['upgrade-apply', '--bundle', str(bundle), '--review-sha256', sha,
                                      '--installed-root', str(root), '--quiescent'])
            result = installation.handle(args, source)
            args = parser.parse_args(['upgrade-rollback', result['operation'], '--installed-root', str(root), '--quiescent'])
            installation.handle(args, source)
            self.assertEqual((root / 'owned.txt').read_bytes(), b'old')
            failed, status = installation.failure('upgrade-apply', installation.UpgradeFailed('published=[owned]; journal=private'), 'profile')
            self.assertEqual(status, 1)
            self.assertIn('published=[owned]; journal=private', failed['result']['reason'])


if __name__ == '__main__':
    unittest.main()
