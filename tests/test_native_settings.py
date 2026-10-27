"""Codex and Claude native project settings have the same preservation rules."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from modding_harness import installation, upgrade


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class NativeSettingsTests(unittest.TestCase):
    def test_settings_parser_retains_bom_support_and_refuses_ambiguous_or_non_json_values(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "settings.json"
            path.write_bytes(b'\xef\xbb\xbf{"permissions":{"deny":["personal"]}}')
            self.assertEqual(upgrade.read_settings(root, path.name), {"permissions": {"deny": ["personal"]}})
            for raw in (b'{"hooks":{},"hooks":{"PreToolUse":[]}}', b'{"timeout":NaN}', b'{"timeout":Infinity}'):
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    upgrade.read_settings(root, path.name)

    def fixture(self, base, custom=False, include=True):
        root, source, bundle = base / "installed", base / "source", base / "review"
        (root / ".codex").mkdir(parents=True)
        source.mkdir()
        old = {"personal": "keep", "hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": "personal post"}]}]}}
        if custom:
            old["hooks"]["PreToolUse"] = [{"matcher": ".*", "hooks": [{"type": "command", "command": "personal pre"}]}]
        (root / ".codex/hooks.json").write_bytes(upgrade.json_bytes(old))
        files = {"runtime.py": b"new runtime"}
        if include:
            files[".codex/hooks.json"] = upgrade.json_bytes({"hooks": {"PreToolUse": []}})
        for name, raw in files.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        review = upgrade.stage_review_bundle(root, source, bundle,
            {"v": 1, "files": {name: sha(raw) for name, raw in files.items()}}, reserve=0)
        return root, bundle, review, old

    def test_custom_codex_pre_policy_blocks_even_an_upgrade_that_keeps_its_file(self):
        for include in (False, True):
            with self.subTest(include=include), tempfile.TemporaryDirectory() as folder:
                root, bundle, review, old = self.fixture(Path(folder), custom=True, include=include)
                self.assertTrue(review["ordered_dispatch_review_required"])
                self.assertEqual(len(review["settings_review"][".codex/hooks.json"]["custom_pre_handlers"]), 1)
                with self.assertRaisesRegex(ValueError, "ordered-policy integration"):
                    installation.apply(root, bundle, sha((bundle / "complete.json").read_bytes()),
                        {".codex/hooks.json": "keep"} if include else {}, quiescent=True, reserve=0)
                self.assertFalse((root / "runtime.py").exists())
                self.assertEqual(upgrade.read_settings(root, ".codex/hooks.json"), old)

    def test_codex_settings_merge_retains_unrelated_content_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            root, bundle, review, old = self.fixture(Path(folder))
            digest = sha((bundle / "complete.json").read_bytes())
            with self.assertRaisesRegex(ValueError, "merged_settings"):
                installation.apply(root, bundle, digest, {".codex/hooks.json": "candidate"}, quiescent=True, reserve=0)
            result = installation.apply(root, bundle, digest, {".codex/hooks.json": "merged_settings"}, quiescent=True, reserve=0)
            merged = upgrade.read_settings(root, ".codex/hooks.json")
            self.assertEqual(merged["personal"], "keep")
            self.assertEqual(merged["hooks"]["PostToolUse"], old["hooks"]["PostToolUse"])
            installation.rollback(root, result["operation"], quiescent=True)
            self.assertEqual(upgrade.read_settings(root, ".codex/hooks.json"), old)

    def test_complete_reference_is_scoped_to_its_original_native_settings_file(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root, source, bundle = base / "installed", base / "source", base / "review"
            (root / ".codex").mkdir(parents=True)
            (source / ".codex").mkdir(parents=True)
            group = {"matcher": ".*", "hooks": [{"type": "command", "command": "same text, different host"}]}
            (root / ".codex/hooks.json").write_bytes(upgrade.json_bytes({"hooks": {"PreToolUse": [group]}}))
            raw = upgrade.json_bytes({"hooks": {"PreToolUse": []}})
            (source / ".codex/hooks.json").write_bytes(raw)
            review = upgrade.stage_review_bundle(root, source, bundle,
                {"v": 1, "files": {".codex/hooks.json": sha(raw)}}, reserve=0,
                owned_pre_groups={".claude/settings.json": [group]})
            self.assertTrue(review["ordered_dispatch_review_required"])

    def test_unchanged_previous_file_qualifies_complete_old_pre_groups(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root, source, bundle = base / "installed", base / "source", base / "review"
            (root / ".codex").mkdir(parents=True)
            (source / ".codex").mkdir(parents=True)
            old = upgrade.json_bytes({"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [
                {"type": "command", "command": "previous toolkit", "timeout": 25}]}]}})
            new = upgrade.json_bytes({"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [
                {"type": "command", "command": "current toolkit", "timeout": 30}]}]}})
            (root / ".codex/hooks.json").write_bytes(old)
            (source / ".codex/hooks.json").write_bytes(new)
            review = upgrade.stage_review_bundle(root, source, bundle,
                {"v": 1, "files": {".codex/hooks.json": sha(new)}},
                {"v": 1, "files": {".codex/hooks.json": sha(old)}}, reserve=0)
            self.assertFalse(review["ordered_dispatch_review_required"])
            merged = upgrade.read_settings(bundle, "merged-codex-hooks.json")
            self.assertEqual(merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "current toolkit")
