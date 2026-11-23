"""The update review: which changes are guard changes, and how they are shown to the user.

Fixtures are hand-written `complete.json` shapes (the keys `upgrade.stage_review_bundle` writes), not a real staging
run, so each test names exactly the row it is about.
"""
import hashlib
import json
import shlex
import tempfile
import unittest
from pathlib import Path

from modding_harness import update_review
from modding_harness.update_review import UpdatePolicy, classify, render_review

HOOK = ".claude/hooks/guard.py"
POLICY_FILE = "tools/update_policy.json"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def policy():
    return UpdatePolicy(guard_paths=(".claude/hooks/*", ".claude/settings.json"), policy_files=(POLICY_FILE,),
                        setup=("python", "setup.py"), canary=("python", "canary.py"),
                        render_config=lambda root: root / "render.json")


class Fixture:
    """installed/, source/ and a review bundle holding a hand-written complete.json."""

    def __init__(self, base):
        self.base = Path(base)
        self.installed = self.base / "installed"
        self.source = self.base / "source"
        self.bundle = self.base / "review"
        for folder in (self.installed, self.source, self.bundle):
            folder.mkdir()
        self.rows = []
        self.merged = {}

    def row(self, path, action, *, installed=None, previous=None, candidate=None):
        for folder, data in ((self.installed, installed), (self.source, candidate)):
            if data is not None:
                target = folder / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        self.rows.append({"path": path, "action": action,
                          "installed_sha256": None if installed is None else sha(installed),
                          "candidate_sha256": None if candidate is None else sha(candidate),
                          "previous_sha256": None if previous is None else sha(previous)})

    def write(self):
        complete = {"v": 1, "mode": "review bundle only; install unchanged",
                    "plan": {"v": 1, "root": str(self.installed), "mode": "read-only preview", "files": self.rows,
                             "collisions": sum(r["action"] == "preserve_collision" for r in self.rows)},
                    "candidate_files": {r["path"]: r["candidate_sha256"] for r in self.rows if r["candidate_sha256"]},
                    "merged_settings_hashes": self.merged}
        path = self.bundle / "complete.json"
        path.write_text(json.dumps(complete, indent=1), encoding="utf-8")
        return sha(path.read_bytes())

    def render(self, **kwargs):
        digest = self.write()
        return render_review(bundle=self.bundle, installed_root=self.installed, source=self.source,
                             policy=policy(), review_sha256=digest, **kwargs), digest


class ClassifyTests(unittest.TestCase):
    def test_unchanged_upstream_guard_file_is_not_a_guard_change(self):
        # The user edited the hook; the new release did not touch it (candidate == previous).
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            fx.row(HOOK, "preserve_collision", installed=b"user edit\n", previous=b"old\n", candidate=b"old\n")
            guard, other = classify(fx.rows, policy())
            self.assertEqual(guard, [])
            self.assertEqual([r["path"] for r in other], [HOOK])
            (text, accept), _ = fx.render()
            self.assertFalse(accept)                         # keep is lossless and stays free
            self.assertIn("unchanged upstream, keep is lossless", text)
            # Choosing `candidate` would replace the user's own hook: that diff is shown under its own heading.
            conditional = text.split(update_review.CONDITIONAL_HEADING, 1)[1].split("Other changes", 1)[0]
            self.assertIn(HOOK, conditional)
            self.assertIn("-user edit", conditional)
            self.assertIn("+old", conditional)
            self.assertNotIn("GUARD CHANGES", text)

    def test_deleted_guard_hook_that_apply_would_recreate_is_a_guard_change(self):
        # The user removed a hook both releases ship: preview emits `create` (candidate == previous, installed absent)
        # and apply writes it back, so the exemption for "unchanged upstream" must not cover it.
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            fx.row(HOOK, "create", previous=b"print('guard')\n", candidate=b"print('guard')\n")
            guard, other = classify(fx.rows, policy())
            self.assertEqual([r["path"] for r in guard], [HOOK])
            self.assertEqual(other, [])
            (text, accept), _ = fx.render()
            self.assertTrue(accept)
            self.assertIn("+print('guard')", text)
            self.assertIn("@@", text)

    def test_kept_local_guard_file_unchanged_upstream_is_not_a_guard_change(self):
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            fx.row(HOOK, "preserve_local", installed=b"mine\n", previous=b"old\n", candidate=b"old\n")
            guard, _ = classify(fx.rows, policy())
            self.assertEqual(guard, [])
            self.assertEqual([r["path"] for r in update_review.conditional_guard_rows(fx.rows, policy())], [HOOK])
            (text, accept), _ = fx.render()
            self.assertFalse(accept)
            self.assertIn("-mine", text.split(update_review.CONDITIONAL_HEADING, 1)[1])

    def test_conditional_rows_are_only_kept_guard_files_whose_candidate_differs(self):
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            fx.row(POLICY_FILE, "preserve_collision", installed=b"{1}\n", previous=b"{0}\n", candidate=b"{0}\n")
            fx.row(".claude/hooks/same.py", "preserve_collision", installed=b"x\n", previous=b"y\n", candidate=b"x\n")
            fx.row("docs/a.md", "preserve_collision", installed=b"mine\n", previous=b"old\n", candidate=b"old\n")
            fx.row(".claude/hooks/changed.py", "preserve_collision", installed=b"m\n", previous=b"o\n", candidate=b"n\n")
            names = [r["path"] for r in update_review.conditional_guard_rows(fx.rows, policy())]
            self.assertEqual(names, [POLICY_FILE])          # same bytes: no change; docs: not guard; changed: a guard row

    def test_guard_row_needs_different_bytes_not_just_a_matching_path(self):
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            fx.row(HOOK, "unchanged", installed=b"same\n", previous=b"same\n", candidate=b"same\n")
            fx.row(POLICY_FILE, "unchanged", installed=b"{}\n", previous=b"{}\n", candidate=b"{}\n")
            fx.row("docs/a.md", "replace_verified_owned", installed=b"a\n", previous=b"a\n", candidate=b"b\n")
            guard, other = classify(fx.rows, policy())
            self.assertEqual(guard, [])
            self.assertEqual(len(other), 3)

    def test_glob_matching_is_case_insensitive_and_scoped(self):
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            fx.row(".Claude/Hooks/Guard.py", "replace_verified_owned", installed=b"a", previous=b"a", candidate=b"b")
            fx.row("hooks/other.py", "replace_verified_owned", installed=b"a", previous=b"a", candidate=b"b")
            guard, other = classify(fx.rows, policy())
            self.assertEqual([r["path"] for r in guard], [".Claude/Hooks/Guard.py"])
            self.assertEqual([r["path"] for r in other], ["hooks/other.py"])


class RenderTests(unittest.TestCase):
    def test_changed_hook_renders_unified_diff_and_requires_accept(self):
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            fx.row(HOOK, "replace_verified_owned", installed=b"a = 1\nkeep\n", previous=b"a = 1\nkeep\n",
                   candidate=b"a = 2\nkeep\n")
            (text, accept), _ = fx.render()
            self.assertTrue(accept)
            self.assertIn("-a = 1", text)
            self.assertIn("+a = 2", text)
            self.assertIn("@@", text)
            self.assertIn(HOOK, text)

    def test_added_and_removed_guard_files_both_listed_and_require_accept(self):
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            fx.row(".claude/hooks/new_hook.py", "create", candidate=b"print('new')\n")
            fx.row(".claude/hooks/old_hook.py", "retain_retired", installed=b"print('old')\n", previous=b"print('old')\n")
            (text, accept), _ = fx.render()
            self.assertTrue(accept)
            self.assertIn(".claude/hooks/new_hook.py", text)
            self.assertIn("+print('new')", text)
            self.assertIn(".claude/hooks/old_hook.py", text)
            self.assertIn("-print('old')", text)

    def test_changed_policy_file_requires_accept(self):
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            fx.row(POLICY_FILE, "replace_verified_owned", installed=b'{"canary": 1}\n', previous=b'{"canary": 1}\n',
                   candidate=b'{"canary": 2}\n')
            (text, accept), _ = fx.render()
            self.assertTrue(accept)
            self.assertIn('+{"canary": 2}', text)

    def test_binary_guard_file_renders_hashes(self):
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            old, new = b"\x00\x01old", b"\x00\x02new"
            fx.row(".claude/hooks/tool.bin", "replace_verified_owned", installed=old, previous=old, candidate=new)
            (text, accept), _ = fx.render()
            self.assertTrue(accept)
            self.assertIn(sha(old), text)
            self.assertIn(sha(new), text)
            self.assertNotIn("@@", text)

    def test_non_utf8_guard_file_shows_hashes_too(self):
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            old, new = b"caf\xe9 1\n", b"caf\xe8 1\n"  # both decode to the same text with replacement characters
            fx.row(HOOK, "replace_verified_owned", installed=old, previous=old, candidate=new)
            (text, accept), _ = fx.render()
            self.assertTrue(accept)
            self.assertIn(sha(old), text)
            self.assertIn(sha(new), text)

    def test_diff_cap_names_hidden_line_count(self):
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            fx.row(".claude/hooks/big.py", "create", candidate="".join("line %d\n" % i for i in range(1000)).encode())
            (text, accept), _ = fx.render()
            self.assertTrue(accept)
            # 2 file headers + 1 hunk header + 1000 added lines = 1003; 400 are shown.
            self.assertIn("603 more lines", text)
            self.assertIn("+line 0", text)
            self.assertNotIn("+line 999", text)

    def command(self, text):
        lines = text.splitlines()
        return next(lines[i + 1] for i, line in enumerate(lines) if line.strip().endswith("run exactly:")).strip()

    def test_next_block_has_exact_command_hash_and_flag_only_when_required(self):
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            fx.row(HOOK, "replace_verified_owned", installed=b"a\n", previous=b"a\n", candidate=b"b\n")
            (text, accept), digest = fx.render()
            self.assertTrue(accept)
            block = text[text.index("next:"):]
            self.assertEqual(shlex.split(self.command(text)), [
                "update-apply", "--bundle", fx.bundle.resolve().as_posix(), "--review-sha256", digest, "--choices",
                (fx.bundle.resolve().parent / "choices.json").as_posix(), "--accept-guard-changes", "--quiescent"])
            self.assertIn("do not run it until the user says go", block.lower())
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            fx.row("docs/a.md", "replace_verified_owned", installed=b"a\n", previous=b"a\n", candidate=b"b\n")
            (text, accept), digest = fx.render()
            self.assertFalse(accept)
            block = text[text.index("next:"):]
            self.assertEqual(shlex.split(self.command(text))[-1], "--quiescent")
            self.assertNotIn("--accept-guard-changes", text)
            self.assertIn("do not run it until the user says go", block.lower())

    def test_printed_command_survives_bash_for_the_default_windows_path_shape(self):
        # The default updates folder has no spaces but (on Windows) backslashes; bash would eat them unquoted.
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            fx.row("docs/a.md", "create", candidate=b"x\n")
            (text, _), digest = fx.render()
            argv = shlex.split(self.command(text))
            self.assertNotIn(" ", str(fx.bundle.resolve()))                      # the shape under test
            self.assertEqual(Path(argv[argv.index("--bundle") + 1]), fx.bundle.resolve())
            choices = argv[argv.index("--choices") + 1]
            self.assertEqual(Path(choices), fx.bundle.resolve().parent / "choices.json")
            self.assertNotIn("<", self.command(text))                            # no placeholder a shell would redirect
            self.assertIn(choices, text.split("run exactly:", 1)[0])             # the step that says to write it names it
        for odd in ("it's here", "a $HOME b", "back`tick", "(x86) dir"):
            with self.subTest(odd), tempfile.TemporaryDirectory() as folder:
                fx = Fixture(Path(folder))
                fx.bundle = Path(folder) / odd / "review"
                fx.bundle.mkdir(parents=True)
                fx.row("docs/a.md", "create", candidate=b"x\n")
                (text, _), digest = fx.render()
                argv = shlex.split(self.command(text))
                self.assertEqual(Path(argv[argv.index("--bundle") + 1]), fx.bundle.resolve())

    def test_policy_paths_are_validated(self):
        good = dict(policy_files=(POLICY_FILE,), setup=("x",), canary=("x",), render_config=lambda root: root)
        for bad in ((), (".claude\\hooks\\*",), ("/abs/*",), ("C:/x/*",), ("a/../b",), ("",), (5,), ".claude/*"):
            with self.subTest(bad), self.assertRaises(ValueError):
                UpdatePolicy(guard_paths=bad, **good)
        for bad in (("tools\\p.json",), ("/p.json",), ("../p.json",), ("",)):
            with self.subTest(bad), self.assertRaises(ValueError):
                UpdatePolicy(guard_paths=(".claude/*",), **{**good, "policy_files": bad})
        UpdatePolicy(guard_paths=(".claude/hooks/*",), **{**good, "policy_files": ()})

    def test_review_prints_glob_coverage_and_warns_for_a_glob_that_matches_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            fx.row(HOOK, "unchanged", installed=b"a\n", previous=b"a\n", candidate=b"a\n")
            fx.row("docs/a.md", "create", candidate=b"x\n")
            (text, accept), _ = fx.render()
            self.assertFalse(accept)                                             # a warning, never a refusal
            self.assertIn("guard globs matched 1 of 2 paths", text)
            self.assertIn("WARNING: guard glob .claude/settings.json matched no path in this release", text)
            self.assertNotIn("WARNING: guard glob .claude/hooks/*", text)

    def test_non_guard_changes_alone_do_not_require_accept(self):
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            fx.row("docs/a.md", "replace_verified_owned", installed=b"a\n", previous=b"a\n", candidate=b"b\n")
            fx.row("docs/b.md", "create", candidate=b"new\n")
            fx.row("docs/c.md", "preserve_collision", installed=b"mine\n", previous=b"old\n", candidate=b"new\n")
            fx.row("docs/same.md", "unchanged", installed=b"s\n", previous=b"s\n", candidate=b"s\n")
            (text, accept), _ = fx.render()
            self.assertFalse(accept)
            for name, action, data in (("docs/a.md", "replace_verified_owned", b"b\n"), ("docs/b.md", "create", b"new\n"),
                                       ("docs/c.md", "preserve_collision", b"new\n")):
                self.assertIn(name, text)
                self.assertIn(action, text)
                self.assertIn(sha(data)[:12], text)
            self.assertIn("1 unchanged", text)

    def test_changed_merged_settings_require_accept_and_show_a_diff(self):
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            settings = ".claude/settings.json"
            fx.row(settings, "preserve_collision", installed=b'{"hooks": 1}\n', previous=b'{"hooks": 0}\n',
                   candidate=b'{"hooks": 2}\n')
            merged = b'{"hooks": 3}\n'
            (fx.bundle / "merged-settings.json").write_bytes(merged)
            fx.merged[settings] = sha(merged)
            (text, accept), _ = fx.render()
            self.assertTrue(accept)
            self.assertIn('+{"hooks": 3}', text)

    def test_unchanged_merged_settings_are_not_a_change(self):
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            settings = ".claude/settings.json"
            body = b'{"hooks": 1}\n'
            fx.row(settings, "unchanged", installed=body, previous=body, candidate=body)
            (fx.bundle / "merged-settings.json").write_bytes(body)
            fx.merged[settings] = sha(body)
            (text, accept), _ = fx.render()
            self.assertFalse(accept)

    def test_rendered_candidate_in_the_bundle_is_what_is_diffed(self):
        # A rendered hook differs from the release's template; the staged bytes are what apply installs.
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            fx.row(HOOK, "replace_verified_owned", installed=b"root = 'old'\n", previous=b"root = 'old'\n",
                   candidate=b"root = '{{ROOT}}'\n")
            rendered = b"root = 'C:/real'\n"
            staged = fx.bundle / "candidate" / HOOK
            staged.parent.mkdir(parents=True)
            staged.write_bytes(rendered)
            fx.rows[0]["candidate_sha256"] = sha(rendered)
            (text, accept), _ = fx.render()
            self.assertTrue(accept)
            self.assertIn("+root = 'C:/real'", text)
            self.assertNotIn("{{ROOT}}", text)

    def test_refuses_review_hash_that_is_not_the_bundles_complete_json(self):
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            fx.row("docs/a.md", "create", candidate=b"x\n")
            fx.write()
            with self.assertRaises(ValueError):
                render_review(bundle=fx.bundle, installed_root=fx.installed, source=fx.source, policy=policy(),
                              review_sha256="0" * 64)
            with self.assertRaises(ValueError):
                render_review(bundle=fx.bundle, installed_root=fx.installed, source=fx.source, policy=policy(),
                              review_sha256="--quiescent")

    def test_refuses_source_bytes_that_do_not_match_the_reviewed_candidate_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            fx.row(HOOK, "replace_verified_owned", installed=b"a\n", previous=b"a\n", candidate=b"b\n")
            (fx.source / HOOK).write_bytes(b"tampered\n")
            with self.assertRaises(ValueError):
                fx.render()

    def test_refuses_row_paths_that_escape_the_roots(self):
        with tempfile.TemporaryDirectory() as folder:
            fx = Fixture(folder)
            fx.rows.append({"path": "../outside.py", "action": "create", "installed_sha256": None,
                            "candidate_sha256": sha(b"x"), "previous_sha256": None})
            with self.assertRaises(ValueError):
                fx.render()

    def test_module_exposes_only_the_documented_interface(self):
        for name in ("UpdatePolicy", "classify", "render_review"):
            self.assertTrue(hasattr(update_review, name))


if __name__ == "__main__":
    unittest.main()
