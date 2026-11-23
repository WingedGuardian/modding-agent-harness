"""Guarded update, apply half: setup and canary after apply, automatic rollback, and a fail-closed marker.

Setup and canary are tiny real scripts run through `sys.executable` (no shell). They live outside the install, append
one JSON line per run to a log, and pass or fail by a rule given on their command line, so every test can say which
steps ran, in which order, in which folder and with which arguments. The fake install and fake release are the stage
tests' own (`tests/test_guarded_update_stage.py`, through the `http` seam of `tests/test_release.py`).
"""
import hashlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest import mock

from modding_harness import guarded_update, installation, upgrade
from modding_harness.locking import exclusive
from modding_harness.update_review import UpdatePolicy
from test_guarded_update_stage import FAILED, HOOK, NEW, OLD, POLICY_FILE, Base, sha

STEP = textwrap.dedent("""\
    import json, os, sys
    from pathlib import Path
    log, name, rule = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(log, "a", encoding="utf-8") as stream:
        stream.write(json.dumps([name, sys.argv[4:], os.getcwd()]) + "\\n")
    print("{} output line 1".format(name))
    print("{} output last line".format(name))
    if rule == "pass":
        sys.exit(0)
    if rule == "fail-if-v2":                # the new hook is broken: fail only while it is installed
        sys.exit(3 if Path(".claude/hooks/guard.py").read_bytes() == b"guard v2\\n" else 0)
    if rule.startswith("write-then-fail:"):
        Path(rule.split(":", 1)[1]).write_text("made by setup\\n")
        sys.exit(4)
    print("{} failed on purpose".format(name), file=sys.stderr)
    sys.exit(5)
    """)


class ApplyBase(Base):
    def setUp(self):
        super().setUp()
        self.scripts = Path(self.tmp.name) / "scripts"
        self.scripts.mkdir()
        self.script = self.scripts / "step.py"
        self.script.write_text(STEP, encoding="utf-8")
        self.log = Path(self.tmp.name) / "steps.log"

    def step(self, name, rule="pass", *extra):
        return (sys.executable, str(self.script), str(self.log), name, rule, *extra)

    def policy(self, setup="pass", canary="pass", canary_name="canary"):
        return UpdatePolicy(guard_paths=(".claude/hooks/*",), policy_files=(POLICY_FILE,),
                            setup=self.step("setup", setup), canary=self.step(canary_name, canary),
                            render_config=lambda root: root / "render.json")

    def steps(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def names(self):
        return [entry[0] for entry in self.steps()]

    def installed(self):
        """Every installed byte outside the harness's own state folder (journals live there)."""
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*")
                if p.is_file() and ".modding-agent-harness" not in p.relative_to(self.root).parts}

    def staged(self, extra=None, policy=None, **kwargs):
        return self.stage(self.github(extra=extra), policy=policy or self.policy(), **kwargs)

    def apply(self, staged, policy=None, choices=None, **kwargs):
        kwargs.setdefault("quiescent", True)
        return guarded_update.apply_update(self.root, policy or self.policy(), staged.bundle, staged.review_sha256,
                                           {"setup.sh": "keep"} if choices is None else choices, **kwargs)

    def journals(self):
        folder = self.root / ".modding-agent-harness/upgrades"
        return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


class RefusalTests(ApplyBase):
    def assert_nothing_happened(self, before):
        self.assertEqual(self.installed(), before)
        self.assertEqual(self.journals(), [])
        self.assertEqual(self.steps(), [])
        self.assertFalse((self.root / FAILED).exists())

    def test_refuses_without_quiescent(self):
        staged = self.staged()
        before = self.installed()
        for value in (False, None, 1, "yes"):
            with self.subTest(value), self.assertRaisesRegex(guarded_update.UpdateRefused, "quiescent"):
                self.apply(staged, quiescent=value)
        self.assert_nothing_happened(before)

    def test_refuses_guard_change_without_accept_flag(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        self.assertTrue(staged.requires_guard_accept)
        before = self.installed()
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "accept-guard-changes"):
            self.apply(staged)
        self.assert_nothing_happened(before)

    def test_refuses_guard_change_when_meta_json_was_edited_to_hide_it(self):
        # meta.json lives in a user-writable folder; the guard decision is re-derived from complete.json.
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        meta = staged.bundle.parent / guarded_update.META
        data = json.loads(meta.read_text(encoding="utf-8"))
        data["requires_guard_accept"] = False
        meta.write_text(json.dumps(data), encoding="utf-8")
        before = self.installed()
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "accept-guard-changes"):
            self.apply(staged)
        self.assert_nothing_happened(before)
        # Even with the flag, a meta.json that disagrees with the review it sits beside is refused.
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "meta.json"):
            self.apply(staged, accept_guard_changes=True)
        self.assert_nothing_happened(before)

    def test_refuses_when_meta_json_names_another_review_or_is_missing(self):
        staged = self.staged()
        meta = staged.bundle.parent / guarded_update.META
        data = json.loads(meta.read_text(encoding="utf-8"))
        meta.write_text(json.dumps({**data, "review_sha256": "0" * 64}), encoding="utf-8")
        before = self.installed()
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "meta.json"):
            self.apply(staged)
        meta.unlink()
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "meta.json"):
            self.apply(staged)
        self.assert_nothing_happened(before)

    def test_refuses_when_review_sha_differs(self):
        staged = self.staged()
        before = self.installed()
        for wrong in ("0" * 64, staged.review_sha256.upper(), staged.review_sha256[:-1], ""):
            with self.subTest(wrong), self.assertRaises(guarded_update.UpdateRefused):
                guarded_update.apply_update(self.root, self.policy(), staged.bundle, wrong, {"setup.sh": "keep"},
                                            quiescent=True)
        self.assert_nothing_happened(before)

    def test_refuses_when_choices_missing_for_a_collision_or_settings_key(self):
        # The settings file is the previous release's own bytes and unchanged in the new one, so it is NOT a collision:
        # only the merged-settings rule can ask for its choice.
        settings, data = ".claude/settings.json", b'{"hooks": {}}\n'
        (self.root / settings).write_bytes(data)
        previous = {OLD + HOOK: self.files[HOOK], OLD + POLICY_FILE: self.files[POLICY_FILE], OLD + settings: data}
        staged = self.stage(self.github(extra={NEW + settings: data}, previous_extra=previous), policy=self.policy())
        review = json.loads((staged.bundle / "complete.json").read_text())
        self.assertIn(settings, review["merged_settings_hashes"])
        actions = {row["path"]: row["action"] for row in review["plan"]["files"]}
        self.assertEqual((actions[settings], actions["setup.sh"]), ("unchanged", "preserve_collision"))
        before = self.installed()
        # The message is this module's own, not installation.apply's later "unresolved installed collision".
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "explicit choice; missing: setup.sh;"):
            self.apply(staged, choices={settings: "keep"}, accept_guard_changes=True)
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "explicit choice; missing: .claude/settings.json;"):
            self.apply(staged, choices={"setup.sh": "keep"}, accept_guard_changes=True)
        self.assert_nothing_happened(before)
        applied = self.apply(staged, choices={"setup.sh": "keep", settings: "keep"}, accept_guard_changes=True)
        self.assertTrue(applied.ok, applied.report)

    def test_lock_held_refuses_cleanly(self):
        staged = self.staged()
        before = self.installed()
        state = self.root / ".modding-agent-harness"
        state.mkdir(exist_ok=True)
        with exclusive(state / "upgrade.lock", 1):
            with self.assertRaisesRegex(guarded_update.UpdateRefused, "upgrade.lock"):
                self.apply(staged)
        self.assertEqual(self.installed(), before)
        self.assertEqual(self.journals(), [])
        self.assertEqual(self.steps(), [])
        self.assertFalse((self.root / FAILED).exists())

    def test_refuses_while_failed_marker_present(self):
        staged = self.staged()
        marker = self.root / FAILED
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{"v": 1}')
        before = self.installed()
        # This module's own refusal, before anything is read; not installation.apply's later stale-review refusal.
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "an earlier update failed"):
            self.apply(staged)
        self.assertEqual(self.installed(), before)
        self.assertEqual(self.journals(), [])
        self.assertEqual(self.steps(), [])

    def test_changed_policy_without_the_new_policy_refuses(self):
        staged = self.staged({NEW + POLICY_FILE: b'{"canary": "B"}\n'})
        before = self.installed()
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "new_policy"):
            self.apply(staged, accept_guard_changes=True)
        self.assert_nothing_happened(before)


class RunTests(ApplyBase):
    def test_success_runs_setup_then_canary_and_reports(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        applied = self.apply(staged, accept_guard_changes=True, notes_files=("NOTES.md",))
        self.assertTrue(applied.ok, applied.report)
        self.assertFalse(applied.rolled_back)
        self.assertRegex(applied.operation, r"^[0-9a-f]{32}$")
        self.assertEqual(self.journals(), [applied.operation])
        self.assertEqual(self.names(), ["setup", "canary"])
        self.assertTrue(all(Path(entry[2]).resolve() == self.root.resolve() for entry in self.steps()))  # cwd
        self.assertEqual((self.root / HOOK).read_bytes(), b"guard v2\n")
        self.assertEqual((self.root / "setup.sh").read_bytes(), b"old\n")            # collision kept
        for name in ("CLAUDE.local.md", "KNOWLEDGEBASE.local.md", "NOTES.md"):
            self.assertEqual((self.root / name).read_bytes(), self.files[name])
        report = applied.report
        self.assertIn(applied.operation, report)
        self.assertIn("4.2.0", report)
        self.assertIn("4.3.0", report)
        self.assertIn("setup.sh", report.split("Collisions kept", 1)[1].split("\n\n", 1)[0])
        self.assertIn("CLAUDE.local.md", report.split("Local files preserved", 1)[1].split("\n\n", 1)[0])
        self.assertIn("setup output last line", report)                              # output tails, not only codes
        self.assertIn("canary output last line", report)
        self.assertFalse((self.root / FAILED).exists())

    def test_canary_failure_rolls_back_to_byte_identical_and_reruns_old_canary(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        before = self.installed()
        applied = self.apply(staged, policy=self.policy(canary="fail-if-v2"), accept_guard_changes=True)
        self.assertFalse(applied.ok)
        self.assertTrue(applied.rolled_back)
        self.assertEqual(self.names(), ["setup", "canary", "canary"])                # new run failed, old one after rollback
        self.assertEqual(self.installed(), before)                                   # every byte, nothing left over
        self.assertFalse((self.root / FAILED).exists())
        self.assertIn(applied.operation, applied.report)
        self.assertIn("canary", applied.report)
        self.assertIn("rolled back", applied.report)
        self.assertIn("Not restored by rollback:\n  (no file under the install root)", applied.report)  # none listed

    def test_setup_failure_rolls_back_the_same_way(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        before = self.installed()
        applied = self.apply(staged, policy=self.policy(setup="write-then-fail:generated.txt"),
                             accept_guard_changes=True)
        self.assertFalse(applied.ok)
        self.assertTrue(applied.rolled_back)
        self.assertEqual(self.names(), ["setup", "canary"])                          # no new canary; old canary after rollback
        self.assertEqual((self.root / "generated.txt").read_text(), "made by setup\n")
        after = self.installed()
        after.pop("generated.txt")
        self.assertEqual(after, before)
        not_restored = applied.report.split("Not restored by rollback", 1)[1]
        self.assertIn("generated.txt", not_restored)
        self.assertFalse((self.root / FAILED).exists())

    def test_missing_executable_counts_as_failure(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        before = self.installed()
        missing = str(Path(self.tmp.name) / "no such tool" / "missing-executable.exe")
        policy = UpdatePolicy(guard_paths=(".claude/hooks/*",), policy_files=(POLICY_FILE,), setup=(missing,),
                              canary=self.step("canary"), render_config=lambda root: root / "render.json")
        applied = self.apply(staged, policy=policy, accept_guard_changes=True)
        self.assertFalse(applied.ok)
        self.assertTrue(applied.rolled_back)
        self.assertEqual(self.installed(), before)
        self.assertIn("could not start", applied.report)
        self.assertEqual(self.names(), ["canary"])                                   # only the old canary, after rollback

    def test_timeout_counts_as_failure(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        before = self.installed()
        seen = []

        def run(argv, cwd, timeout):
            seen.append((tuple(argv), Path(cwd), timeout))
            if "canary" in argv and len(seen) == 2:
                raise subprocess.TimeoutExpired(argv, timeout, output=b"partial canary output\n")
            return subprocess.CompletedProcess(argv, 0, b"fine\n", b"")
        applied = self.apply(staged, accept_guard_changes=True, run=run)
        self.assertFalse(applied.ok)
        self.assertTrue(applied.rolled_back)
        self.assertEqual(self.installed(), before)
        self.assertEqual([entry[2] for entry in seen], [guarded_update.TIMEOUT] * 3)
        self.assertTrue(all(entry[1] == self.root.resolve() for entry in seen))
        self.assertIn("timed out", applied.report)
        self.assertIn("partial canary output", applied.report)

    def test_changed_policy_requires_old_and_new_canary(self):
        staged = self.staged({NEW + POLICY_FILE: b'{"canary": "B"}\n'})
        new = self.policy(canary_name="new-canary")
        applied = self.apply(staged, accept_guard_changes=True, new_policy=new)
        self.assertTrue(applied.ok, applied.report)
        self.assertEqual(self.names(), ["setup", "new-canary", "canary"])
        # The old canary failing is enough to roll back, even though the new one passes.
        self._undo_last_apply()
        staged = self.staged({NEW + POLICY_FILE: b'{"canary": "B"}\n'})
        before = self.installed()
        old = self.policy(canary="fail")
        applied = self.apply(staged, policy=old, accept_guard_changes=True, new_policy=new)
        self.assertFalse(applied.ok)
        self.assertTrue(applied.rolled_back)
        self.assertEqual(self.installed(), before)
        self.assertEqual(self.names(), ["setup", "new-canary", "canary", "canary"])
        self.assertTrue((self.root / FAILED).exists())     # the old canary fails on the restored state too: fail closed

    def _undo_last_apply(self):
        """Undo the first apply so the same fake release can be staged and applied again."""
        operation = self.journals()[-1]
        installation.rollback(self.root, operation, quiescent=True)
        self.log.unlink()

    def test_double_failure_writes_marker_and_stage_refuses_until_cleared(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        applied = self.apply(staged, policy=self.policy(canary="fail"), accept_guard_changes=True)
        self.assertFalse(applied.ok)
        self.assertTrue(applied.rolled_back)
        self.assertEqual(self.names(), ["setup", "canary", "canary"])
        marker = json.loads((self.root / FAILED).read_text(encoding="utf-8"))
        self.assertEqual(marker["operation"], applied.operation)
        self.assertEqual(marker["to_version"], "4.3.0")
        self.assertEqual(marker["from_version"], "4.2.0")
        self.assertEqual(marker["failed_step"], "canary after rollback")
        self.assertIn("canary failed on purpose", marker["output_tail"])
        self.assertIn(FAILED, applied.report)
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "update-failed"):
            self.staged()
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "update-failed"):
            self.apply(staged, accept_guard_changes=True)
        guarded_update.clear_failed(self.root)
        self.assertFalse((self.root / FAILED).exists())
        kept = list((self.root / ".modding-agent-harness").glob("update-failed.cleared-*.json"))
        self.assertEqual(len(kept), 1)                                               # cleared, not destroyed
        self.staged()                                                                # staging works again
        guarded_update.clear_failed(self.root)                                       # nothing to clear: no error

    def test_rollback_failure_writes_marker(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        with mock.patch.object(installation, "rollback", side_effect=installation.UpgradeFailed("disk gone")):
            applied = self.apply(staged, policy=self.policy(canary="fail"), accept_guard_changes=True)
        self.assertFalse(applied.ok)
        self.assertFalse(applied.rolled_back)
        marker = json.loads((self.root / FAILED).read_text(encoding="utf-8"))
        self.assertEqual(marker["failed_step"], "rollback")
        self.assertEqual(marker["operation"], applied.operation)
        self.assertIn("disk gone", marker["error"])
        self.assertIn(applied.operation, applied.report)
        self.assertEqual(self.names(), ["setup", "canary"])                          # no canary on an unrestored state

    def test_partial_apply_failure_rolls_back_what_was_published(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        before = self.installed()
        real = installation.durable.atomic_json

        def fail_on_second_publish(path, data):
            if Path(path).name == "000002-published.json":
                raise OSError("simulated crash after a partial publish")
            return real(path, data)
        with mock.patch.object(installation.durable, "atomic_json", side_effect=fail_on_second_publish):
            applied = self.apply(staged, accept_guard_changes=True)
        self.assertFalse(applied.ok)
        self.assertTrue(applied.rolled_back)
        self.assertRegex(applied.operation, r"^[0-9a-f]{32}$")
        self.assertEqual(self.installed(), before)
        self.assertEqual(self.names(), ["canary"])                                   # old canary on the restored state
        self.assertIn("simulated crash", applied.report)

    def test_marker_survives_a_later_upgrade(self):
        """The marker is a retained local file: an ordinary upgrade neither overwrites it nor goes stale around it."""
        root, source, bundle = Path(self.tmp.name) / "plain", Path(self.tmp.name) / "src", Path(self.tmp.name) / "rev"
        for folder in (root, source):
            (folder / ".modding-agent-harness").mkdir(parents=True)
        marker = b'{"v": 1, "failed_step": "canary after rollback"}\n'
        (root / FAILED).write_bytes(marker)
        (root / "owned.txt").write_bytes(b"old")
        (source / FAILED).write_bytes(b"shipped")
        (source / "owned.txt").write_bytes(b"new")
        inventory = lambda files: {"v": 1, "files": {name: sha(data) for name, data in files.items()}}
        upgrade.stage_review_bundle(root, source, bundle, inventory({FAILED: b"shipped", "owned.txt": b"new"}),
                                    inventory({"owned.txt": b"old"}), reserve=0)
        review = hashlib.sha256((bundle / "complete.json").read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "local settings/notes must be retained"):
            installation.apply(root, bundle, review, {FAILED: "candidate"}, quiescent=True, reserve=0)
        self.assertEqual((root / FAILED).read_bytes(), marker)
        installation.apply(root, bundle, review, {FAILED: "keep"}, quiescent=True, reserve=0)
        self.assertEqual((root / "owned.txt").read_bytes(), b"new")
        self.assertEqual((root / FAILED).read_bytes(), marker)

    def test_a_marker_written_during_a_review_makes_the_review_stale(self):
        root, source, bundle = Path(self.tmp.name) / "plain", Path(self.tmp.name) / "src", Path(self.tmp.name) / "rev"
        for folder in (root, source):
            folder.mkdir()
        (root / "owned.txt").write_bytes(b"old")
        (source / "owned.txt").write_bytes(b"new")
        inventory = lambda data: {"v": 1, "files": {"owned.txt": sha(data)}}
        upgrade.stage_review_bundle(root, source, bundle, inventory(b"new"), inventory(b"old"), reserve=0)
        review = hashlib.sha256((bundle / "complete.json").read_bytes()).hexdigest()
        (root / FAILED).parent.mkdir()
        (root / FAILED).write_bytes(b'{"v": 1}')
        with self.assertRaisesRegex(ValueError, "changed since review"):
            installation.apply(root, bundle, review, quiescent=True, reserve=0)
        self.assertEqual((root / "owned.txt").read_bytes(), b"old")

    def test_argv_with_spaces_and_parens_runs_intact(self):
        odd = Path(self.tmp.name) / "Program Files (x86)" / "tool kit"
        odd.mkdir(parents=True)
        script = odd / "step (1).py"
        script.write_text(STEP, encoding="utf-8")
        args = ("a b", "(x86)", 'quote"d', "&echo nope", "%PATH%", "")
        argv = (sys.executable, str(script), str(self.log), "setup", "pass", *args)
        policy = UpdatePolicy(guard_paths=(".claude/hooks/*",), policy_files=(POLICY_FILE,), setup=argv,
                              canary=self.step("canary"), render_config=lambda root: root / "render.json")
        applied = self.apply(self.staged(), policy=policy)
        self.assertTrue(applied.ok, applied.report)
        self.assertEqual(self.steps()[0][1], list(args))

    def test_default_run_uses_no_shell_and_no_console_window(self):
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"", b"")) as fake:
            guarded_update.run(["x", "y z"], self.root, 7)
        args, kwargs = fake.call_args
        self.assertEqual(args[0], ["x", "y z"])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["timeout"], 7)
        self.assertEqual(Path(kwargs["cwd"]), self.root)
        self.assertTrue(kwargs["capture_output"])
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        if sys.platform == "win32":
            self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)


if __name__ == "__main__":
    import unittest
    unittest.main()
