"""Destination rendering is independent, bounded and bound to the reviewed bytes."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from modding_harness import installation, upgrade


def sha(data):
    return hashlib.sha256(data).hexdigest()


class CandidateRenderTests(unittest.TestCase):
    def fixture(self, base):
        root, source, bundle = base / "installed", base / "source", base / "review"
        root.mkdir()
        source.mkdir()
        (root / "AGENTS.md").write_bytes(b"private previous instructions")
        template = b"Game root: {{ROOT}}\n"
        (source / "AGENTS.md").write_bytes(template)
        return root, source, bundle, template, {"v": 1, "files": {"AGENTS.md": sha(template)}}

    def test_rendered_identity_controls_preview_apply_and_rollback_without_editing_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, template, inventory = self.fixture(Path(folder))
            rendered = b"Game root: destination [fixture]\n"
            def prepare(installed, staged, files):
                self.assertEqual(installed, root)
                self.assertNotEqual(staged, source)
                self.assertEqual(files, inventory["files"])
                return {"AGENTS.md": rendered}
            result = upgrade.stage_review_bundle(root, source, bundle, inventory,
                                                 candidate_transform=prepare, reserve=0)
            self.assertEqual((source / "AGENTS.md").read_bytes(), template)
            self.assertEqual((root / "AGENTS.md").read_bytes(), b"private previous instructions")
            self.assertEqual(result["source_files"], inventory["files"])
            self.assertEqual(result["candidate_files"], {"AGENTS.md": sha(rendered)})
            self.assertEqual(result["plan"]["files"][0]["candidate_sha256"], sha(rendered))
            self.assertEqual(result["rendered_files"], ["AGENTS.md"])
            checked = []
            def validate(installed, rows):
                checked.extend(rows)
                self.assertEqual(Path(rows[0]["source"]).read_bytes(), rendered)
                rows[0]["after"] = "a" * 64  # callback does not receive mutable execution authority
            applied = installation.apply(root, bundle, sha((bundle / "complete.json").read_bytes()),
                {"AGENTS.md": "candidate"}, quiescent=True, reserve=0, candidate_validator=validate)
            self.assertEqual(len(checked), 1)
            self.assertEqual((root / "AGENTS.md").read_bytes(), rendered)
            installation.rollback(root, applied["operation"], quiescent=True)
            self.assertEqual((root / "AGENTS.md").read_bytes(), b"private previous instructions")

    def test_unknown_paths_wrong_types_and_oversized_rendering_leave_no_completion_marker(self):
        for updates in ({"../foreign": b"bad"}, {"new.txt": b"not inventoried"},
                        {"AGENTS.md": "not bytes"}, {"AGENTS.md": b"x" * (8 * 1024 * 1024 + 1)}):
            with self.subTest(paths=list(updates)), tempfile.TemporaryDirectory() as folder:
                root, source, bundle, template, inventory = self.fixture(Path(folder))
                with self.assertRaises(ValueError):
                    upgrade.stage_review_bundle(root, source, bundle, inventory,
                        candidate_transform=lambda *_: updates, reserve=0)
                self.assertFalse((bundle / "complete.json").exists())
                self.assertEqual((source / "AGENTS.md").read_bytes(), template)
                self.assertEqual((root / "AGENTS.md").read_bytes(), b"private previous instructions")

    def test_apply_validator_refuses_unrendered_candidate_before_any_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, template, inventory = self.fixture(Path(folder))
            upgrade.stage_review_bundle(root, source, bundle, inventory, reserve=0)
            def validate(installed, rows):
                if any(b"{{ROOT}}" in Path(row["source"]).read_bytes() for row in rows):
                    raise ValueError("candidate needs destination rendering")
            with self.assertRaisesRegex(ValueError, "destination rendering"):
                installation.apply(root, bundle, sha((bundle / "complete.json").read_bytes()),
                    {"AGENTS.md": "candidate"}, quiescent=True, reserve=0, candidate_validator=validate)
            self.assertFalse((root / ".modding-agent-harness").exists())

    def test_changed_source_during_preparation_invalidates_review(self):
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, _, inventory = self.fixture(Path(folder))
            def prepare(*_):
                (source / "AGENTS.md").write_bytes(b"concurrent source edit")
                return {"AGENTS.md": b"rendered"}
            with self.assertRaisesRegex(OSError, "review source changed"):
                upgrade.stage_review_bundle(root, source, bundle, inventory, candidate_transform=prepare, reserve=0)
            self.assertFalse((bundle / "complete.json").exists())

    def test_readonly_source_is_rendered_only_in_owned_independent_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, template, inventory = self.fixture(Path(folder))
            path = source / "AGENTS.md"
            path.chmod(0o444)
            original_mode = path.stat().st_mode
            try:
                upgrade.stage_review_bundle(root, source, bundle, inventory,
                    candidate_transform=lambda *_: {"AGENTS.md": b"rendered"}, reserve=0)
                self.assertEqual(path.stat().st_mode, original_mode)
                self.assertEqual(path.read_bytes(), template)
                self.assertFalse((bundle / "candidate/AGENTS.md").stat().st_mode & 0o222)
            finally:
                path.chmod(0o644)
                candidate = bundle / "candidate/AGENTS.md"
                if candidate.exists():
                    candidate.chmod(0o644)

    def test_cli_render_config_requires_profile_support_and_uses_bounded_json(self):
        parser = argparse.ArgumentParser()
        installation.add_arguments(parser.add_subparsers(dest="command", required=True))
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, template, inventory = self.fixture(Path(folder))
            manifest, config = Path(folder) / "candidate.json", Path(folder) / "render.json"
            manifest.write_text(json.dumps(inventory))
            config.write_text('{"v":1,"root":"destination"}')
            args = parser.parse_args(["upgrade-stage", "--installed-root", str(root), "--source", str(source),
                "--candidate", str(manifest), "--destination", str(bundle), "--render-config", str(config)])
            with self.assertRaisesRegex(ValueError, "no candidate renderer"):
                installation.handle(args, root)
            self.assertFalse(bundle.exists())
            def prepare(installed, staged, files, data):
                self.assertEqual(data, {"v": 1, "root": "destination"})
                return {"AGENTS.md": template.replace(b"{{ROOT}}", data["root"].encode())}
            result = installation.handle(args, root, candidate_preparer=prepare)
            self.assertEqual(result["rendered_files"], ["AGENTS.md"])
            self.assertEqual((source / "AGENTS.md").read_bytes(), template)

    def test_declared_generated_definition_has_reviewed_collision_and_rollback_authority(self):
        name = ".codex/hooks.json"
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, template, inventory = self.fixture(Path(folder))
            (root / ".codex").mkdir()
            old = b'{"hooks":{"PreToolUse":[]},"personal":"retained for review"}'
            (root / name).write_bytes(old)
            new = b'{"hooks":{"PreToolUse":[]}}'
            result = upgrade.stage_review_bundle(root, source, bundle, inventory,
                candidate_transform=lambda *_: {name: new}, generated_paths=(name,), reserve=0)
            self.assertEqual(result["generated_files"], [name])
            self.assertNotIn(name, result["source_files"])
            self.assertEqual(result["rollback_files"][name], sha(old))
            row = next(row for row in result["plan"]["files"] if row["path"] == name)
            self.assertEqual(row["action"], "preserve_collision")
            digest = sha((bundle / "complete.json").read_bytes())
            with self.assertRaisesRegex(ValueError, "unresolved installed collision"):
                installation.apply(root, bundle, digest, {"AGENTS.md": "keep"}, quiescent=True, reserve=0)
            applied = installation.apply(root, bundle, digest, {"AGENTS.md": "keep", name: "merged_settings"}, quiescent=True, reserve=0)
            self.assertEqual(json.loads((root / name).read_bytes())["personal"], "retained for review")
            installation.rollback(root, applied["operation"], quiescent=True)
            self.assertEqual((root / name).read_bytes(), old)

    def test_profile_generation_cannot_add_private_notes_settings_or_recovery_files(self):
        for name in ("KNOWLEDGEBASE.local.md", ".claude/settings.local.json", ".modding-agent-harness/backups/foreign"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root, source, bundle, _, inventory = self.fixture(Path(folder))
                with self.assertRaisesRegex(ValueError, "unsupported generated configuration"):
                    upgrade.stage_review_bundle(root, source, bundle, inventory,
                        candidate_transform=lambda *_: {name: b"bad"}, generated_paths=(name,), reserve=0)
                self.assertFalse(bundle.exists())

    def test_generated_destination_created_during_rendering_invalidates_review(self):
        with tempfile.TemporaryDirectory() as folder:
            root, source, bundle, _, inventory = self.fixture(Path(folder))
            def prepare(*_):
                (root / ".codex").mkdir()
                (root / ".codex/hooks.json").write_bytes(b"foreign creator")
                return {".codex/hooks.json": b'{"hooks":{}}'}
            with self.assertRaisesRegex(OSError, "local configuration changed"):
                upgrade.stage_review_bundle(root, source, bundle, inventory, candidate_transform=prepare,
                                            generated_paths=(".codex/hooks.json",), reserve=0)
            self.assertEqual((root / ".codex/hooks.json").read_bytes(), b"foreign creator")
            self.assertFalse((bundle / "complete.json").exists())
