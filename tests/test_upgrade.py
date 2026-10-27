import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest

from modding_harness import upgrade


def manifest(**files):
    return {"v": 1, "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}


class UpgradeTests(unittest.TestCase):
    def test_internal_directory_redirects_cannot_supply_upgrade_authority(self):
        for location in ("installed", "source"):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as folder:
                base = Path(folder)
                root, source = base / "installed", base / "source"
                root.mkdir()
                source.mkdir()
                redirected = (root if location == "installed" else source)
                target = redirected / "user-owned"
                target.mkdir()
                (target / "owned.txt").write_bytes(b"private unrelated implementation")
                link = redirected / "tools"
                if os.name == "nt":
                    shell = shutil.which("powershell")
                    self.assertIsNotNone(shell, "Windows junction qualification requires PowerShell")
                    script = base / "junction.ps1"
                    script.write_text("param($Link,$Target)\nNew-Item -ItemType Junction -Path $Link -Target $Target -ErrorAction Stop | Out-Null\n")
                    subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(script), str(link), str(target)],
                                   check=True, capture_output=True, timeout=10)
                else:
                    link.symlink_to(target, target_is_directory=True)
                if location == "installed":
                    (source / "tools").mkdir()
                    (source / "tools/owned.txt").write_bytes(b"replacement")
                data = (source / "tools/owned.txt").read_bytes()
                with self.assertRaisesRegex(ValueError, "link|redirect"):
                    upgrade.stage_review_bundle(root, source, base / "review",
                        manifest(**{"tools/owned.txt": data}), reserve=0)
                self.assertEqual((target / "owned.txt").read_bytes(), b"private unrelated implementation")
                self.assertFalse((base / "review/complete.json").exists())

    def test_review_bundle_retains_exact_private_bytes_and_detects_local_pre_handler(self):
        import json
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root, source, output = base / "installed", base / "source", base / "review"
            for tree in (root, source):
                (tree / ".claude").mkdir(parents=True)
            old_settings = b'{"model":"personal","hooks":{"PreToolUse":[{"hooks":[{"type":"command","command":"old owned"}]}]}}'
            new_settings = b'{"hooks":{"PreToolUse":[{"hooks":[{"type":"command","command":"new dispatcher"}]}]}}'
            local = b'{"permissions":{"deny":["private"]},"hooks":{"PreToolUse":[{"hooks":[{"type":"command","command":"personal restriction"}]}]}}'
            (root / ".claude/settings.json").write_bytes(old_settings)
            (source / ".claude/settings.json").write_bytes(new_settings)
            (root / ".claude/settings.local.json").write_bytes(local)
            (root / "AGENTS.md").write_bytes(b"personal instructions")
            (root / ".claude/personal-policy.py").write_bytes(b"personal restriction source")
            (source / "AGENTS.md").write_bytes(b"candidate instructions")
            candidate = manifest(**{".claude/settings.json": new_settings, "AGENTS.md": b"candidate instructions"})
            result = upgrade.stage_review_bundle(root, source, output, candidate,
                owned_commands=("old owned",), local_files=(".claude/personal-policy.py",), reserve=0,
                owned_pre_groups={".claude/settings.json": json.loads(old_settings)["hooks"]["PreToolUse"]})
            self.assertTrue(result["ordered_dispatch_review_required"])
            self.assertFalse(result["application_authorized"])
            self.assertEqual((output / "rollback/.claude/settings.local.json").read_bytes(), local)
            self.assertEqual((output / "rollback/.claude/personal-policy.py").read_bytes(), b"personal restriction source")
            self.assertEqual((root / ".claude/settings.json").read_bytes(), old_settings)
            self.assertEqual((root / "AGENTS.md").read_bytes(), b"personal instructions")
            merged = json.loads((output / "merged-settings.json").read_bytes())
            self.assertEqual(merged["model"], "personal")
            self.assertEqual(merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "new dispatcher")
            self.assertTrue((output / "complete.json").is_file())
            (root / "AGENTS.md").write_bytes(b"later user edit")
            self.assertEqual((output / "rollback/AGENTS.md").read_bytes(), b"personal instructions")
            with self.assertRaises(FileExistsError):
                upgrade.stage_review_bundle(root, source, output, candidate, reserve=0)

    def test_review_bundle_refuses_candidate_hash_drift_and_live_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root, source = base / "installed", base / "source"
            root.mkdir()
            source.mkdir()
            (source / "file").write_bytes(b"actual")
            with self.assertRaises(ValueError):
                upgrade.stage_review_bundle(root, source, base / "review", manifest(file=b"wrong"), reserve=0)
            self.assertFalse((base / "review").exists())
            with self.assertRaises(ValueError):
                upgrade.stage_review_bundle(root, source, root / "review", manifest(file=b"actual"), reserve=0)
            for local in ("../outside", "FILE"):
                with self.subTest(local=local), self.assertRaises(ValueError):
                    upgrade.stage_review_bundle(root, source, base / "review", manifest(file=b"actual"),
                                                local_files=(local,), reserve=0)

    def test_review_bundle_source_race_leaves_no_complete_marker(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root, source, output = base / "installed", base / "source", base / "review"
            root.mkdir()
            source.mkdir()
            (source / "file").write_bytes(b"actual")
            real = upgrade.durable.copy_verified
            def race(src, dst, deadline):
                result = real(src, dst, deadline)
                (source / "file").write_bytes(b"changed")
                return result
            with mock.patch.object(upgrade.durable, "copy_verified", side_effect=race):
                with self.assertRaises(OSError):
                    upgrade.stage_review_bundle(root, source, output, manifest(file=b"actual"), reserve=0)
            self.assertFalse((output / "complete.json").exists())
            self.assertEqual(list(root.iterdir()), [])

    def test_low_space_never_creates_a_notes_snapshot_or_local_preferences(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = root / "KNOWLEDGEBASE.md"
            original.write_bytes(b"personal notes")
            usage = upgrade.shutil.disk_usage(root)
            with mock.patch.object(upgrade.shutil, "disk_usage", return_value=usage._replace(free=10)):
                with self.assertRaises(OSError):
                    upgrade.preserve_notes(root, "a" * 64, reserve=10)
            self.assertEqual([p.name for p in root.iterdir()], ["KNOWLEDGEBASE.md"])
            self.assertEqual(original.read_bytes(), b"personal notes")

    def test_legacy_notes_retained_independently_and_never_inferred_as_preferences(self):
        from modding_harness import preferences
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            legacy = b"# Prior notes\n## My preferences\nThis old heading remains unclassified evidence.\n"
            (root / "KNOWLEDGEBASE.md").write_bytes(legacy)
            result = upgrade.preserve_notes(root, "a" * 64, reserve=0)
            self.assertEqual(Path(result["snapshot"]).read_bytes(), legacy)
            self.assertEqual(preferences.render(root), "")
            (root / "KNOWLEDGEBASE.md").write_bytes(b"replacement")
            self.assertEqual(Path(result["snapshot"]).read_bytes(), legacy)
            local = root / "KNOWLEDGEBASE.local.md"
            local.write_bytes(b"## My preferences\nExisting user preference\n")
            upgrade.preserve_notes(root, "a" * 64, reserve=0)
            self.assertEqual(local.read_bytes(), b"## My preferences\nExisting user preference\n")

    def test_notes_reuse_corruption_refuses_and_unchanged_candidate_is_noop(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = root / "KNOWLEDGEBASE.md"
            original.write_bytes(b"private notes")
            digest = hashlib.sha256(original.read_bytes()).hexdigest()
            self.assertFalse(upgrade.preserve_notes(root, digest, reserve=0)["preserved"])
            result = upgrade.preserve_notes(root, "a" * 64, reserve=0)
            again = upgrade.preserve_notes(root, "a" * 64, reserve=0)
            self.assertEqual(result["snapshot"], again["snapshot"])
            Path(result["snapshot"]).write_bytes(b"corrupt")
            with self.assertRaises(OSError):
                upgrade.preserve_notes(root, "a" * 64, reserve=0)
            self.assertEqual(original.read_bytes(), b"private notes")

    def test_changed_unknown_and_local_files_are_preserved_and_retired_files_never_deleted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, data in {"owned": b"old", "modified": b"user edit", "local": b"notes",
                               "unknown": b"personal", "retired": b"retained"}.items():
                (root / name).write_bytes(data)
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            old = manifest(owned=b"old", modified=b"old", local=b"notes", retired=b"retained")
            new = manifest(owned=b"new", modified=b"new", local=b"new", unknown=b"new", created=b"new")
            result = upgrade.preview(root, new, old, keep_local=("local",))
            self.assertEqual({r["path"]: r["action"] for r in result["files"]}, {
                "owned": "replace_verified_owned", "modified": "preserve_collision", "local": "preserve_local",
                "unknown": "preserve_collision", "created": "create", "retired": "retain_retired"})
            self.assertEqual(result["collisions"], 2)
            self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)

    def test_missing_baseline_never_invents_ownership(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "same").write_bytes(b"identical")
            (Path(folder) / "changed").write_bytes(b"private")
            result = upgrade.preview(folder, manifest(same=b"identical", changed=b"new"))
            self.assertEqual([r["action"] for r in result["files"]], ["preserve_collision", "unchanged"])

    def test_unsafe_or_case_colliding_inventory_refuses(self):
        for names in ({"../x": "a" * 64}, {"C:/x": "a" * 64}, {"a\\b": "a" * 64},
                      {"/abs": "a" * 64}, {"a/./b": "a" * 64}, {"a": "a" * 64, "A": "b" * 64}):
            with self.subTest(names=names), self.assertRaises(ValueError):
                upgrade.inventory({"v": 1, "files": names})

    def test_custom_handlers_and_non_hook_settings_survive_exact_owned_replacement(self):
        def group(command):
            return {"matcher": ".*", "hooks": [{"type": "command", "command": command}]}
        existing = {"permissions": {"deny": ["personal rule"]}, "model": "user choice",
                    "hooks": {"PreToolUse": [group("old toolkit"), group("local policy")]}}
        candidate = {"hooks": {"PreToolUse": [group("new dispatcher")]}, "model": "shipped default"}
        merged = upgrade.merge_settings(existing, candidate, ["old toolkit"], owned_pre_groups=[group("old toolkit")])
        self.assertEqual(merged["settings"]["permissions"], existing["permissions"])
        self.assertEqual(merged["settings"]["model"], "user choice")
        self.assertEqual(merged["settings"]["hooks"]["PreToolUse"], [group("local policy"), group("new dispatcher")])
        self.assertTrue(merged["ordered_dispatch_review_required"])
        self.assertEqual(len(existing["hooks"]["PreToolUse"]), 2)

    def test_familiar_posix_command_does_not_retire_an_unowned_windows_policy(self):
        handler = {"type": "command", "command": "old toolkit bash", "commandWindows": "personal windows policy"}
        existing = {"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [handler]}]}}
        result = upgrade.merge_settings(existing, {}, ["old toolkit bash"])
        self.assertEqual(result["custom_pre_handlers"], [handler])
        self.assertTrue(result["ordered_dispatch_review_required"])

    def test_identical_complete_candidate_groups_do_not_become_custom_on_repeat(self):
        group = {"matcher": ".*", "hooks": [{"type": "command", "command": "current toolkit", "timeout": 30}]}
        hooks = {"PreToolUse": [group]}
        result = upgrade.merge_settings({"permissions": {"deny": ["personal"]}, "hooks": hooks}, {"hooks": hooks})
        self.assertFalse(result["ordered_dispatch_review_required"])
        self.assertEqual(result["settings"]["permissions"], {"deny": ["personal"]})

    def test_command_string_ownership_cannot_retire_changed_pre_handler_semantics(self):
        for change in ("matcher", "timeout", "async"):
            with self.subTest(change=change):
                handler = {"type": "command", "command": "old toolkit", "timeout": 30}
                group = {"matcher": ".*", "hooks": [handler]}
                if change == "matcher":
                    group["matcher"] = "Write"
                elif change == "timeout":
                    handler["timeout"] = 1
                else:
                    handler["async"] = True
                result = upgrade.merge_settings({"hooks": {"PreToolUse": [group]}}, {}, ["old toolkit"])
                self.assertEqual(result["custom_pre_handlers"], [handler])
                self.assertTrue(result["ordered_dispatch_review_required"])

    def test_complete_group_references_do_not_conflate_boolean_and_number_fields(self):
        known = {"hooks": [{"type": "command", "command": "toolkit", "timeout": 0}]}
        changed = {"hooks": [{"type": "command", "command": "toolkit", "timeout": False}]}
        result = upgrade.merge_settings({"hooks": {"PreToolUse": [changed]}}, {}, owned_pre_groups=[known])
        self.assertTrue(result["ordered_dispatch_review_required"])


if __name__ == "__main__":
    unittest.main()
