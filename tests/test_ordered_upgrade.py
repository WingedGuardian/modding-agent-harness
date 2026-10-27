"""Explicit ordered-policy migration never guesses ownership or host trust."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from modding_harness import durable, installation, upgrade


PROJECT = ".claude/settings.json"
LOCAL = ".claude/settings.local.json"
REGISTRY = ".modding-agent-harness/local-policies.json"
POLICY = ".claude/hooks/personal.py"
RUNTIME = "tools/dispatcher.py"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def group(command):
    return {"matcher": ".*", "hooks": [{"type": "command", "command": command, "timeout": 25}]}


class OrderedUpgradeTests(unittest.TestCase):
    def fixture(self, base, *, builder=None, unchanged_runtime=False, local=False, candidate_group=None):
        root, source, bundle = base / "installed", base / "source", base / "review"
        root.mkdir()
        source.mkdir()
        settings = {"permissions": {"deny": ["personal restriction"]}, "model": "personal choice", "personalFeature": True,
                    "hooks": {"PreToolUse": [group("personal save guard")], "PostToolUse": [group("personal post")]}}
        old = {PROJECT: upgrade.json_bytes(settings), POLICY: b"original pure policy",
               RUNTIME: b"new runtime" if unchanged_runtime else b"old runtime"}
        if local:
            old[LOCAL] = upgrade.json_bytes({"env": {"PERSONAL": "retained"},
                "hooks": {"PreToolUse": [group("personal save guard")]}})
        new = {PROJECT: upgrade.json_bytes({"hooks": {"PreToolUse": [candidate_group or group("ordered dispatcher")]}}),
               RUNTIME: b"new runtime"}
        for parent, files in ((root, old), (source, new)):
            for name, data in files.items():
                path = parent / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        inventory = lambda rows: {"v": 1, "files": {name: digest(data) for name, data in rows.items()}}
        builder = builder or self.builder
        review = upgrade.stage_review_bundle(root, source, bundle, inventory(new), inventory(old),
            local_files=(POLICY,), review_builder=builder, reserve=0)
        sha = digest((bundle / "complete.json").read_bytes())
        choices = {PROJECT: "reviewed_settings", REGISTRY: "reviewed_settings"}
        if local:
            choices[LOCAL] = "reviewed_settings"
        return root, bundle, sha, choices, review, old

    def builder(self, bundle, review):
        settings = upgrade.read_settings(bundle, "merged-settings.json")
        settings["hooks"]["PreToolUse"] = upgrade.read_settings(bundle / "candidate", PROJECT)["hooks"]["PreToolUse"]
        files = {PROJECT: settings, REGISTRY: {"v": 1, "kind": "toy-policy", "sha256": review["rollback_files"][POLICY]}}
        local = upgrade.read_settings(bundle / "rollback", LOCAL)
        if local:
            local.setdefault("hooks", {})["PreToolUse"] = []
            files[LOCAL] = local
        return {"v": 1, "kind": "toy-policy", "files": files,
                "sources": {POLICY: review["rollback_files"][POLICY]},
                "requires": {RUNTIME: review["candidate_files"][RUNTIME]}}

    def test_changed_json_types_cannot_retire_handlers_or_modify_unrelated_settings(self):
        candidate = {"matcher": ".*", "hooks": [{"type": "command", "command": "ordered dispatcher",
                                                "timeout": 1, "async": False}]}
        for field, value in (("timeout", True), ("timeout", 1.0), ("async", 0), ("personalFeature", 1)):
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as folder:
                def builder(bundle, review):
                    proposal = self.builder(bundle, review)
                    settings = proposal["files"][PROJECT]
                    if field == "personalFeature":
                        settings[field] = value
                    else:
                        settings["hooks"]["PreToolUse"][0]["hooks"][0][field] = value
                    return proposal
                with self.assertRaisesRegex(ValueError, "pre-handlers|unrelated settings"):
                    self.fixture(Path(folder), builder=builder, candidate_group=candidate)
                root = Path(folder) / "installed"
                self.assertEqual((root / RUNTIME).read_bytes(), b"old runtime")
                self.assertFalse((root / REGISTRY).exists())

    def test_normal_migration_retains_number_and_boolean_types(self):
        candidate = {"matcher": ".*", "hooks": [{"type": "command", "command": "ordered dispatcher",
                                                "timeout": 1, "async": False}]}
        with tempfile.TemporaryDirectory() as folder:
            root, bundle, sha, choices, _, _ = self.fixture(Path(folder), candidate_group=candidate)
            installation.apply(root, bundle, sha, choices, quiescent=True, reserve=0, review_builder=self.builder)
            settings = json.loads((root / PROJECT).read_bytes())
            handler = settings["hooks"]["PreToolUse"][0]["hooks"][0]
            self.assertIs(type(handler["timeout"]), int)
            self.assertIs(handler["async"], False)
            self.assertIs(settings["personalFeature"], True)

    def test_complete_lifecycle_preserves_policy_permissions_and_local_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root, bundle, sha, choices, review, old = self.fixture(Path(folder), local=True)
            self.assertEqual((root / PROJECT).read_bytes(), old[PROJECT])
            result = installation.apply(root, bundle, sha, choices, quiescent=True, reserve=0, review_builder=self.builder)
            published = json.loads((Path(result["journal"]) / "plan.json").read_bytes())["changes"]
            names = [row["path"] for row in published]
            self.assertLess(names.index(RUNTIME), names.index(REGISTRY))
            self.assertLess(names.index(REGISTRY), names.index(PROJECT))
            settings = upgrade.read_settings(root, PROJECT)
            self.assertEqual(settings["permissions"], {"deny": ["personal restriction"]})
            self.assertEqual(settings["model"], "personal choice")
            self.assertEqual(settings["hooks"]["PostToolUse"], [group("personal post")])
            self.assertEqual(upgrade.read_settings(root, LOCAL)["env"], {"PERSONAL": "retained"})
            self.assertEqual((root / POLICY).read_bytes(), old[POLICY])
            rolled = installation.rollback(root, result["operation"], quiescent=True)
            for name, data in old.items():
                self.assertEqual((root / name).read_bytes(), data)
            self.assertFalse((root / REGISTRY).exists())
            self.assertTrue((Path(rolled["journal"]) / "retained" / REGISTRY).is_file())
            again = installation.rollback(root, result["operation"], quiescent=True)
            self.assertEqual(again["restored"], [])

    def test_incomplete_or_unvalidated_review_refuses_without_publication(self):
        for defect in ("no_validator", "no_choices", "partial_choices", "keep_runtime", "changed_reviewed",
                       "changed_candidate", "changed_rollback", "changed_policy"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as folder:
                root, bundle, sha, choices, _, old = self.fixture(Path(folder))
                builder = self.builder
                if defect == "no_validator":
                    builder = None
                elif defect == "no_choices":
                    choices = {}
                elif defect == "partial_choices":
                    choices.pop(REGISTRY)
                elif defect == "keep_runtime":
                    choices[RUNTIME] = "keep"
                else:
                    target = {"changed_reviewed": bundle / "reviewed" / PROJECT,
                              "changed_candidate": bundle / "candidate" / RUNTIME,
                              "changed_rollback": bundle / "rollback" / POLICY,
                              "changed_policy": root / POLICY}[defect]
                    target.write_bytes(b"foreign change")
                with self.assertRaises((ValueError, OSError)):
                    installation.apply(root, bundle, sha, choices, quiescent=True, reserve=0, review_builder=builder)
                self.assertEqual((root / PROJECT).read_bytes(), old[PROJECT])
                self.assertEqual((root / RUNTIME).read_bytes(), old[RUNTIME])
                self.assertFalse((root / REGISTRY).exists())
                self.assertFalse((root / ".modding-agent-harness/upgrades").exists())

    def test_a_profile_cannot_leave_unknown_pre_handlers_or_change_their_group_semantics(self):
        for defect in ("unknown", "matcher", "timeout", "async"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as folder:
                def builder(bundle, review):
                    proposal = self.builder(bundle, review)
                    pre = proposal["files"][PROJECT]["hooks"]["PreToolUse"]
                    if defect == "unknown":
                        pre.append(group("unrecognized custom policy"))
                    elif defect == "matcher":
                        pre[0]["matcher"] = "Write"
                    else:
                        pre[0]["hooks"][0][defect] = True if defect == "async" else 1
                    return proposal
                with self.assertRaisesRegex(ValueError, "unresolved custom pre-handlers"):
                    self.fixture(Path(folder), builder=builder)

    def test_ordered_migration_cannot_discard_permissions_or_unrelated_handlers(self):
        for target in ("permissions", "PostToolUse"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as folder:
                def builder(bundle, review):
                    proposal = self.builder(bundle, review)
                    settings = proposal["files"][PROJECT]
                    (settings if target == "permissions" else settings["hooks"]).pop(target)
                    return proposal
                with self.assertRaisesRegex(ValueError, "unrelated settings or handlers"):
                    self.fixture(Path(folder), builder=builder)

    def test_proposals_cannot_edit_notes_recovery_or_uncaptured_local_sources(self):
        for defect in ("notes", "recovery", "source", "runtime", "empty_runtime"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as folder:
                def builder(bundle, review):
                    proposal = self.builder(bundle, review)
                    if defect in ("notes", "recovery"):
                        proposal["files"]["KNOWLEDGEBASE.local.md" if defect == "notes" else ".claude/backups/state.json"] = {}
                    elif defect == "source":
                        proposal["sources"] = {"not-captured.py": "a" * 64}
                    elif defect == "runtime":
                        proposal["requires"][RUNTIME] = "a" * 64
                    else:
                        proposal["requires"] = {}
                    return proposal
                with self.assertRaises(ValueError):
                    self.fixture(Path(folder), builder=builder)

    def test_policy_changed_during_publication_refuses_before_first_target_change(self):
        with tempfile.TemporaryDirectory() as folder:
            root, bundle, sha, choices, _, old = self.fixture(Path(folder))
            real = durable.copy_verified
            def race(src, dst, deadline=None):
                result = real(src, dst, deadline)
                if Path(src) == bundle / "candidate" / RUNTIME:
                    (root / POLICY).write_bytes(b"concurrent edit")
                return result
            with mock.patch.object(durable, "copy_verified", side_effect=race):
                with self.assertRaisesRegex(installation.UpgradeFailed, "published=\\[\\]"):
                    installation.apply(root, bundle, sha, choices, quiescent=True, reserve=0, review_builder=self.builder)
            self.assertEqual((root / PROJECT).read_bytes(), old[PROJECT])
            self.assertEqual((root / RUNTIME).read_bytes(), old[RUNTIME])

    def test_readonly_local_settings_are_never_unlocked(self):
        with tempfile.TemporaryDirectory() as folder:
            root, bundle, sha, choices, _, old = self.fixture(Path(folder), local=True)
            path = root / LOCAL
            path.chmod(0o444)
            mode = path.stat().st_mode
            try:
                with self.assertRaisesRegex(ValueError, "read-only"):
                    installation.apply(root, bundle, sha, choices, quiescent=True, reserve=0, review_builder=self.builder)
                self.assertEqual(path.stat().st_mode, mode)
                self.assertEqual((root / PROJECT).read_bytes(), old[PROJECT])
            finally:
                path.chmod(0o644)

    def test_rollback_refuses_foreign_policy_or_unchanged_runtime_before_restoring_anything(self):
        for target in (POLICY, RUNTIME):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as folder:
                root, bundle, sha, choices, _, _ = self.fixture(Path(folder), unchanged_runtime=True)
                result = installation.apply(root, bundle, sha, choices, quiescent=True, reserve=0, review_builder=self.builder)
                (root / target).write_bytes(b"foreign change")
                before = (root / PROJECT).read_bytes()
                with self.assertRaisesRegex(ValueError, "changed before rollback"):
                    installation.rollback(root, result["operation"], quiescent=True)
                self.assertEqual((root / PROJECT).read_bytes(), before)
                self.assertTrue((root / REGISTRY).is_file())
