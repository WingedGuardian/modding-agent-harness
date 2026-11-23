"""Guarded update, apply half: setup and canary after apply, automatic rollback, and a fail-closed marker.

Setup and canary are tiny real scripts run through `sys.executable` (no shell). They live outside the install, append
one JSON line per run to a log, and pass or fail by a rule given on their command line, so every test can say which
steps ran, in which order, in which folder and with which arguments. The fake install and fake release are the stage
tests' own (`tests/test_guarded_update_stage.py`, through the `http` seam of `tests/test_release.py`).
"""
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest import mock

from modding_harness import guarded_update, installation, update_review, upgrade
from modding_harness.locking import exclusive
from modding_harness.update_review import UpdatePolicy
from test_guarded_update_stage import FAILED, HOOK, NEW, OLD, POLICY_FILE, Base, sha

INFLIGHT = ".modding-agent-harness/update-in-flight.json"

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
    if rule == "fail-if-v1":                # the restored (old) state makes this step fail
        sys.exit(6 if Path(".claude/hooks/guard.py").read_bytes() == b"guard v1\\n" else 0)
    if rule.startswith("write-then-fail:"):
        Path(rule.split(":", 1)[1]).write_text("made by setup\\n")
        sys.exit(4)
    if rule.startswith("write-then-fail-if-v2:"):
        Path(rule.split(":", 1)[1]).write_text("made by setup\\n")
        sys.exit(4 if Path(".claude/hooks/guard.py").read_bytes() == b"guard v2\\n" else 0)
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
        self.assertFalse((self.root / INFLIGHT).exists())                           # a clean refusal is final

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
        # new canary failed; then the installed setup re-deploys from the restored files, then the old canary
        self.assertEqual(self.names(), ["setup", "canary", "setup", "canary"])
        self.assertEqual(self.installed(), before)                                   # every byte, nothing left over
        self.assertIn("re-ran from the restored toolkit", applied.report)
        self.assertFalse((self.root / FAILED).exists())
        self.assertIn(applied.operation, applied.report)
        self.assertIn("canary", applied.report)
        self.assertIn("rolled back", applied.report)
        self.assertIn("Not restored by rollback:\n  (no file under the install root)", applied.report)  # none listed

    def test_setup_failure_rolls_back_the_same_way(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        before = self.installed()
        applied = self.apply(staged, policy=self.policy(setup="write-then-fail-if-v2:generated.txt"),
                             accept_guard_changes=True)
        self.assertFalse(applied.ok)
        self.assertTrue(applied.rolled_back)
        self.assertEqual(self.names(), ["setup", "setup", "canary"])                 # no new canary; setup + old canary
        self.assertEqual(self.steps()[1][0], "setup")
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
        # The installed setup is the missing one, so re-deploying the restored toolkit fails too: fail closed.
        marker = json.loads((self.root / FAILED).read_text(encoding="utf-8"))
        self.assertEqual(marker["failed_step"], "setup after rollback")
        self.assertEqual(self.names(), [])                                           # no canary on an undeployed state

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
        self.assertEqual([entry[2] for entry in seen], [guarded_update.TIMEOUT] * 4)
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
        self.assertEqual(self.names(), ["setup", "new-canary", "canary", "setup", "canary"])
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
        self.assertEqual(self.names(), ["setup", "canary", "setup", "canary"])
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
        self.assertEqual(self.names(), ["setup", "canary"])                          # re-deploy, then the old canary
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
        real = subprocess.Popen
        seen = []

        def spy(argv, **kwargs):
            seen.append((argv, kwargs))
            return real(argv, **kwargs)
        with mock.patch.object(subprocess, "Popen", side_effect=spy):
            done = guarded_update.run([sys.executable, "-c", "print('hi')"], self.root, 30)
        argv, kwargs = seen[0]
        self.assertEqual(argv, [sys.executable, "-c", "print('hi')"])
        self.assertIs(kwargs.get("shell", False), False)
        self.assertEqual(Path(kwargs["cwd"]), self.root)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        for stream in ("stdout", "stderr"):
            self.assertNotIn(kwargs[stream], (subprocess.PIPE, None))              # a file: a grandchild cannot block it
        if sys.platform == "win32":
            self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)
        else:
            self.assertIs(kwargs["start_new_session"], True)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, b"hi"))

    def test_timeout_kills_the_whole_process_tree(self):
        pid_file = Path(self.tmp.name) / "grandchild.pid"
        child = Path(self.tmp.name) / "child.py"
        child.write_text(textwrap.dedent("""\
            import subprocess, sys, time
            grandchild = subprocess.Popen([sys.executable, "-c",
                "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(120)", sys.argv[1]])
            print("child started", flush=True)
            time.sleep(120)
            """), encoding="utf-8")
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            guarded_update.run([sys.executable, str(child), str(pid_file)], self.root, 4)
        self.assertLess(time.monotonic() - started, 60)                                 # it returned, it did not hang
        self.assertIn(b"child started", caught.exception.output or b"")
        grandchild = int(pid_file.read_text())
        deadline = time.monotonic() + 15
        while guarded_update._alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertIs(guarded_update._alive(grandchild), False)                       # the grandchild is gone too


def foreign_journal(root, review="f" * 64):
    """Another updater's operation journal, with a publication intent, as `installation.apply` would leave it."""
    operation = "e" * 32
    journal = root / ".modding-agent-harness/upgrades" / operation
    journal.mkdir(parents=True)
    (journal / "plan.json").write_text(json.dumps({"v": 1, "root": str(root.resolve()), "review_sha256": review,
                                                   "changes": [], "ordered_integration": None}))
    (journal / "000001-intent.json").write_text(json.dumps({"v": 1, "path": "x", "identity": [1, 2, 3, 4, 5]}))
    return journal


class ConcurrencyAndInFlightTests(ApplyBase):
    def files_in(self, folder):
        return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}

    def test_foreign_journal_created_during_the_lock_wait_is_not_rolled_back(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        before = self.installed()
        made = []

        @contextlib.contextmanager
        def busy(path, wait=5.0):
            made.append(foreign_journal(self.root))         # updater B took the lock and published meanwhile
            raise TimeoutError("preservation/cache lock is busy")
            yield
        with mock.patch.object(installation, "exclusive", busy), \
                mock.patch.object(installation, "rollback", wraps=installation.rollback) as rollback:
            with self.assertRaisesRegex(guarded_update.UpdateRefused, "upgrade.lock"):
                self.apply(staged, accept_guard_changes=True)
        self.assertEqual(rollback.call_count, 0)
        foreign = self.files_in(made[0])
        self.assertEqual(sorted(foreign), ["000001-intent.json", "plan.json"])      # untouched, no rollback receipt
        self.assertEqual(self.installed(), before)
        self.assertFalse((self.root / FAILED).exists())
        self.assertFalse((self.root / INFLIGHT).exists())
        self.assertEqual(self.steps(), [])

    def test_partial_failure_rolls_back_only_its_own_operation(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        before = self.installed()
        real = installation.durable.atomic_json
        made = []

        def fail_on_second_publish(path, data):
            if Path(path).name == "000002-published.json":
                made.append(foreign_journal(self.root))     # a second new journal appears before the failure
                raise OSError("simulated crash after a partial publish")
            return real(path, data)
        with mock.patch.object(installation.durable, "atomic_json", side_effect=fail_on_second_publish):
            applied = self.apply(staged, accept_guard_changes=True)
        self.assertFalse(applied.ok)
        self.assertTrue(applied.rolled_back)
        self.assertNotEqual(applied.operation, made[0].name)
        self.assertEqual(self.installed(), before)
        self.assertEqual(sorted(self.files_in(made[0])), ["000001-intent.json", "plan.json"])
        self.assertFalse((self.root / FAILED).exists())
        self.assertFalse((self.root / INFLIGHT).exists())

    def test_an_apply_failure_naming_no_journal_of_ours_fails_closed(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        failure = installation.UpgradeFailed("upgrade failed; published=['x']; journal=/nowhere: boom")
        with mock.patch.object(installation, "apply", side_effect=failure):
            applied = self.apply(staged, accept_guard_changes=True)
        self.assertFalse(applied.ok)
        self.assertFalse(applied.rolled_back)
        self.assertNotIn("nothing was changed", applied.report)
        marker = json.loads((self.root / FAILED).read_text(encoding="utf-8"))
        self.assertEqual(marker["failed_step"], "apply")
        self.assertIn("boom", marker["error"])
        self.assertFalse((self.root / INFLIGHT).exists())                           # the marker took its place

    def test_a_failure_naming_a_foreign_journal_is_not_rolled_back(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        journal = foreign_journal(self.root)                # right place and shape, but another review's plan
        failure = installation.UpgradeFailed("upgrade failed; published=['x']; journal={}: boom".format(journal))
        with mock.patch.object(installation, "apply", side_effect=failure), \
                mock.patch.object(installation, "rollback") as rollback:
            applied = self.apply(staged, accept_guard_changes=True)
        self.assertEqual(rollback.call_count, 0)
        self.assertFalse(applied.ok)
        self.assertEqual(json.loads((self.root / FAILED).read_text(encoding="utf-8"))["failed_step"], "apply")
        self.assertEqual(sorted(self.files_in(journal)), ["000001-intent.json", "plan.json"])

    def test_an_apply_failure_before_any_publication_is_a_clean_refusal(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        before = self.installed()
        real = installation.durable.atomic_json

        def fail_first_intent(path, data):
            if Path(path).name == "000001-intent.json":
                raise OSError("simulated failure before the first publication")
            return real(path, data)
        with mock.patch.object(installation.durable, "atomic_json", side_effect=fail_first_intent):
            with self.assertRaisesRegex(guarded_update.UpdateRefused, "before publishing anything"):
                self.apply(staged, accept_guard_changes=True)
        self.assertEqual(self.installed(), before)
        self.assertEqual(self.steps(), [])
        self.assertFalse((self.root / FAILED).exists())
        self.assertFalse((self.root / INFLIGHT).exists())

    def test_a_kill_during_the_canary_leaves_the_in_flight_record_and_both_steps_refuse(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        during = []

        def run(argv, cwd, timeout):
            if "canary" in argv:
                # What a hard kill (no Python handler runs) would leave: the record as it is on disk right now.
                during.append(json.loads((self.root / INFLIGHT).read_text(encoding="utf-8")))
                raise KeyboardInterrupt
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        with self.assertRaises(KeyboardInterrupt):
            self.apply(staged, accept_guard_changes=True, run=run)
        self.assertEqual(during[0]["operation"], self.journals()[0])
        record = json.loads((self.root / INFLIGHT).read_text(encoding="utf-8"))
        self.assertEqual(record["operation"], self.journals()[0])
        self.assertIn("KeyboardInterrupt", record["interrupted"])
        self.assertEqual(record["review_sha256"], staged.review_sha256)
        self.assertEqual((record["from_version"], record["to_version"]), ("4.2.0", "4.3.0"))
        self.assertIsInstance(record["pid"], int)
        self.assertFalse((self.root / FAILED).exists())
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "update-in-flight"):
            self.staged()
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "update-in-flight"):
            self.apply(staged, accept_guard_changes=True)

    def test_the_record_is_written_before_apply_without_an_operation(self):
        staged = self.staged()
        seen = []
        real = installation.apply

        def spy(*args, **kwargs):
            seen.append(json.loads((self.root / INFLIGHT).read_text(encoding="utf-8")))
            return real(*args, **kwargs)
        with mock.patch.object(installation, "apply", side_effect=spy):
            applied = self.apply(staged)
        self.assertTrue(applied.ok, applied.report)
        self.assertIsNone(seen[0]["operation"])
        self.assertEqual(seen[0]["review_sha256"], staged.review_sha256)

    def test_success_removes_the_record(self):
        applied = self.apply(self.staged())
        self.assertTrue(applied.ok, applied.report)
        self.assertFalse((self.root / INFLIGHT).exists())

    def test_clean_rollback_removes_the_record(self):
        applied = self.apply(self.staged({NEW + HOOK: b"guard v2\n"}), policy=self.policy(canary="fail-if-v2"),
                             accept_guard_changes=True)
        self.assertTrue(applied.rolled_back)
        self.assertFalse((self.root / FAILED).exists())
        self.assertFalse((self.root / INFLIGHT).exists())

    def test_double_failure_leaves_the_marker_and_removes_the_record(self):
        applied = self.apply(self.staged({NEW + HOOK: b"guard v2\n"}), policy=self.policy(canary="fail"),
                             accept_guard_changes=True)
        self.assertFalse(applied.ok)
        self.assertTrue((self.root / FAILED).exists())
        self.assertFalse((self.root / INFLIGHT).exists())

    def test_a_marker_that_cannot_be_written_keeps_the_record(self):
        real = guarded_update._write_marker

        def no_marker(root, data, name=guarded_update.FAILED_MARKER):
            return "OSError: disk full" if name == guarded_update.FAILED_MARKER else real(root, data, name)
        with mock.patch.object(guarded_update, "_write_marker", side_effect=no_marker):
            applied = self.apply(self.staged({NEW + HOOK: b"guard v2\n"}), policy=self.policy(canary="fail"),
                                 accept_guard_changes=True)
        self.assertIn("COULD NOT WRITE", applied.report)
        self.assertTrue((self.root / INFLIGHT).exists())

    def test_clear_failed_clears_both(self):
        for name in (FAILED, INFLIGHT):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"v": 1}')
        guarded_update.clear_failed(self.root, force=True)          # no pid: cannot tell, so only a forced clear
        self.assertFalse((self.root / FAILED).exists())
        self.assertFalse((self.root / INFLIGHT).exists())
        state = self.root / ".modding-agent-harness"
        self.assertEqual(len(list(state.glob("update-failed.cleared-*.json"))), 1)
        self.assertEqual(len(list(state.glob("update-in-flight.cleared-*.json"))), 1)
        self.staged()

    def test_the_record_survives_a_later_upgrade_and_does_not_make_a_review_stale(self):
        record = b'{"v": 1, "operation": null}\n'
        inventory = lambda files: {"v": 1, "files": {name: sha(data) for name, data in files.items()}}

        def plain(name, shipped):
            root, source, bundle = (Path(self.tmp.name) / (name + suffix) for suffix in ("-root", "-src", "-rev"))
            for folder in (root, source):
                (folder / ".modding-agent-harness").mkdir(parents=True)
            (root / "owned.txt").write_bytes(b"old")
            (source / "owned.txt").write_bytes(b"new")
            files = {"owned.txt": b"new"}
            if shipped:
                (source / INFLIGHT).write_bytes(b"shipped")
                files[INFLIGHT] = b"shipped"
            return root, source, bundle, files
        # Retained: a release that ships the name can never replace the user's record.
        root, source, bundle, files = plain("retained", True)
        (root / INFLIGHT).write_bytes(record)
        upgrade.stage_review_bundle(root, source, bundle, inventory(files), inventory({"owned.txt": b"old"}), reserve=0)
        review = hashlib.sha256((bundle / "complete.json").read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "local settings/notes must be retained"):
            installation.apply(root, bundle, review, {INFLIGHT: "candidate"}, quiescent=True, reserve=0)
        self.assertEqual((root / INFLIGHT).read_bytes(), record)
        installation.apply(root, bundle, review, {INFLIGHT: "keep"}, quiescent=True, reserve=0)
        self.assertEqual((root / "owned.txt").read_bytes(), b"new")
        self.assertEqual((root / INFLIGHT).read_bytes(), record)
        # Not observed: writing the record after staging (as apply_update does) must not make the review stale.
        root, source, bundle, files = plain("unobserved", False)
        upgrade.stage_review_bundle(root, source, bundle, inventory(files), inventory({"owned.txt": b"old"}), reserve=0)
        self.assertNotIn(INFLIGHT, json.loads((bundle / "complete.json").read_text())["observed_local_files"])
        review = hashlib.sha256((bundle / "complete.json").read_bytes()).hexdigest()
        (root / INFLIGHT).write_bytes(record)
        installation.apply(root, bundle, review, quiescent=True, reserve=0)
        self.assertEqual((root / "owned.txt").read_bytes(), b"new")
        self.assertEqual((root / INFLIGHT).read_bytes(), record)

    # --- final review wave ---------------------------------------------------------------------------------------

    def put_record(self, data):
        path = self.root / INFLIGHT
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def dead_pid(self):
        done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, check=True)
        return int(done.stdout)

    def test_clear_refuses_while_the_recorded_process_is_alive_unless_forced(self):
        path = self.put_record({"v": 1, "pid": os.getpid(), "started": "2026-11-23T00:00:00Z"})
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "still running"):
            guarded_update.clear_failed(self.root)
        self.assertTrue(path.exists())
        path.write_text('{"v": 1}')                                     # no pid at all: cannot tell, treated as alive
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "still running"):
            guarded_update.clear_failed(self.root)
        guarded_update.clear_failed(self.root, force=True)
        self.assertFalse(path.exists())

    def test_clear_goes_through_when_the_recorded_process_is_gone(self):
        self.assertIs(guarded_update._alive(self.dead_pid()), False)
        path = self.put_record({"v": 1, "pid": self.dead_pid()})
        guarded_update.clear_failed(self.root)
        self.assertFalse(path.exists())

    def test_clear_of_the_failed_marker_alone_needs_no_liveness(self):
        marker = self.root / FAILED
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{"v": 1}')
        guarded_update.clear_failed(self.root)
        self.assertFalse(marker.exists())

    def test_refusal_says_in_progress_or_interrupted_with_pid_and_start(self):
        self.put_record({"v": 1, "pid": 4242, "started": "2026-11-23T01:02:03Z", "operation": "a" * 32})
        with self.assertRaises(guarded_update.UpdateRefused) as caught:
            self.staged()
        message = str(caught.exception)
        self.assertIn("in progress or interrupted", message)
        self.assertIn("pid 4242", message)
        self.assertIn("2026-11-23T01:02:03Z", message)
        self.assertIn("a" * 32, message)

    def test_a_kill_during_publication_names_the_journal_in_the_refusal(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        real = installation.durable.atomic_json

        def killed(path, data):
            if Path(path).name == "000001-published.json":
                raise KeyboardInterrupt
            return real(path, data)
        with mock.patch.object(installation.durable, "atomic_json", side_effect=killed), \
                self.assertRaises(KeyboardInterrupt):
            self.apply(staged, accept_guard_changes=True)
        record = json.loads((self.root / INFLIGHT).read_text(encoding="utf-8"))
        self.assertIsNone(record["operation"])
        (journal,) = self.journals()
        for attempt in (lambda: self.apply(staged, accept_guard_changes=True), self.staged):
            with self.assertRaises(guarded_update.UpdateRefused) as caught:
                attempt()
            self.assertIn(journal, str(caught.exception))
            self.assertNotIn("OPERATION --quiescent", str(caught.exception))

    def test_a_record_another_updater_creates_after_our_check_is_not_overwritten(self):
        staged = self.staged()
        real = guarded_update._snapshot
        foreign = json.dumps({"v": 1, "nonce": "theirs", "pid": os.getpid()})

        def racing(root):
            self.put_record(json.loads(foreign))                     # updater B wins the race for the record
            return real(root)
        with mock.patch.object(guarded_update, "_snapshot", side_effect=racing):
            with self.assertRaisesRegex(guarded_update.UpdateRefused, "update-in-flight"):
                self.apply(staged)
        self.assertEqual(json.loads((self.root / INFLIGHT).read_text(encoding="utf-8"))["nonce"], "theirs")
        self.assertEqual(self.journals(), [])
        self.assertEqual(self.steps(), [])

    def test_settle_removes_only_a_record_that_is_still_ours(self):
        staged = self.staged()
        real = guarded_update._default_run

        def run(argv, cwd, timeout):
            if "canary" in argv:
                self.put_record({"v": 1, "nonce": "theirs"})             # replaced under us
            return real(argv, cwd, timeout)
        applied = self.apply(staged, run=run)
        self.assertTrue(applied.ok, applied.report)
        self.assertEqual(json.loads((self.root / INFLIGHT).read_text(encoding="utf-8"))["nonce"], "theirs")

    def test_a_rewrite_never_overwrites_a_record_that_is_no_longer_ours(self):
        staged = self.staged()
        real = installation.apply

        def apply(*args, **kwargs):
            result = real(*args, **kwargs)
            self.put_record({"v": 1, "nonce": "theirs", "operation": None})      # replaced before our rewrite
            return result
        with mock.patch.object(installation, "apply", side_effect=apply):
            applied = self.apply(staged)
        self.assertTrue(applied.ok, applied.report)
        record = json.loads((self.root / INFLIGHT).read_text(encoding="utf-8"))
        self.assertEqual((record["nonce"], record["operation"]), ("theirs", None))   # not rewritten, not removed

    def test_record_rewrites_keep_the_nonce(self):
        staged = self.staged()
        seen = []

        def run(argv, cwd, timeout):
            seen.append(json.loads((self.root / INFLIGHT).read_text(encoding="utf-8")))
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        self.apply(staged, run=run)
        self.assertRegex(seen[0]["nonce"], r"^[0-9a-f]{32}$")
        self.assertIsNotNone(seen[0]["operation"])                   # rewritten with the operation, nonce kept

    def cleanup_fault(self, name):
        """installation.apply fails at `name`, then its own cleanup raises and replaces the UpgradeFailed."""
        real_json, real_apply = installation.durable.atomic_json, installation.apply

        def fault(path, data):
            if Path(path).name == name:
                raise OSError("simulated failure at " + name)
            return real_json(path, data)

        def apply(*args, **kwargs):
            try:
                return real_apply(*args, **kwargs)
            except installation.UpgradeFailed:
                raise PermissionError("cleanup of a temp file was blocked by a scanner")
        return mock.patch.multiple(installation, apply=mock.DEFAULT), fault, apply

    def test_a_cleanup_fault_after_a_partial_publish_still_recovers(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        before = self.installed()
        _, fault, apply = self.cleanup_fault("000002-published.json")
        with mock.patch.object(installation.durable, "atomic_json", side_effect=fault), \
                mock.patch.object(installation, "apply", side_effect=apply):
            applied = self.apply(staged, accept_guard_changes=True)
        self.assertFalse(applied.ok)
        self.assertTrue(applied.rolled_back)
        self.assertEqual(self.installed(), before)
        self.assertNotIn("nothing was changed", applied.report)
        self.assertFalse((self.root / INFLIGHT).exists())

    def test_a_cleanup_fault_with_a_journal_but_no_publication_keeps_the_record(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        before = self.installed()
        _, fault, apply = self.cleanup_fault("000001-intent.json")
        with mock.patch.object(installation.durable, "atomic_json", side_effect=fault), \
                mock.patch.object(installation, "apply", side_effect=apply):
            applied = self.apply(staged, accept_guard_changes=True)
        self.assertFalse(applied.ok)
        self.assertNotIn("nothing was changed", applied.report)
        self.assertEqual(self.installed(), before)
        self.assertTrue((self.root / INFLIGHT).exists())             # a journal of ours exists: not a clean refusal
        self.assertEqual(self.steps(), [])

    def test_a_plain_refusal_with_no_journal_of_ours_is_still_clean(self):
        staged = self.staged()
        foreign_journal(self.root)                                     # another review's journal does not count
        with mock.patch.object(installation, "apply", side_effect=PermissionError("denied before anything")):
            with self.assertRaisesRegex(guarded_update.UpdateRefused, "nothing was changed"):
                self.apply(staged)
        self.assertFalse((self.root / INFLIGHT).exists())

    def test_setup_after_rollback_failing_writes_the_marker(self):
        staged = self.staged({NEW + HOOK: b"guard v2\n"})
        applied = self.apply(staged, policy=self.policy(setup="fail-if-v1", canary="fail-if-v2"),
                             accept_guard_changes=True)
        self.assertFalse(applied.ok)
        self.assertTrue(applied.rolled_back)
        self.assertEqual(self.names(), ["setup", "canary", "setup"])  # no old canary on an undeployed state
        marker = json.loads((self.root / FAILED).read_text(encoding="utf-8"))
        self.assertEqual(marker["failed_step"], "setup after rollback")
        self.assertFalse((self.root / INFLIGHT).exists())


class GuardChoiceTests(ApplyBase):
    """Critical 1: a choice of `candidate` on a guard file the review exempted (the user's own edit, unchanged
    upstream) replaces guard code, so it needs the guard go; `keep` stays free."""

    def edit(self, name, data):
        (self.root / name).write_bytes(data)

    def test_candidate_on_a_locally_edited_hook_needs_the_guard_go(self):
        self.edit(HOOK, b"guard v1 with my own rule\n")
        staged = self.staged()
        self.assertFalse(staged.requires_guard_accept)               # the review itself still needs no go
        self.assertIn(update_review.CONDITIONAL_HEADING, staged.text)
        self.assertIn("-guard v1 with my own rule", staged.text)
        before = self.installed()
        choices = {"setup.sh": "keep", HOOK: "candidate"}
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "accept-guard-changes") as caught:
            self.apply(staged, choices=choices)
        self.assertIn(HOOK, str(caught.exception))
        self.assertEqual(self.installed(), before)
        self.assertFalse((self.root / INFLIGHT).exists())
        applied = self.apply(staged, choices=choices, accept_guard_changes=True)
        self.assertTrue(applied.ok, applied.report)
        self.assertEqual((self.root / HOOK).read_bytes(), b"guard v1\n")

    def test_keep_on_a_locally_edited_hook_stays_free(self):
        self.edit(HOOK, b"guard v1 with my own rule\n")
        applied = self.apply(self.staged(), choices={"setup.sh": "keep", HOOK: "keep"})
        self.assertTrue(applied.ok, applied.report)
        self.assertEqual((self.root / HOOK).read_bytes(), b"guard v1 with my own rule\n")

    def test_candidate_on_a_kept_local_guard_file_needs_the_guard_go(self):
        # stage_update passes no keep_local, so the upgrader's keep-local row is produced by wrapping its stage call.
        self.edit(HOOK, b"guard v1 with my own rule\n")
        real = upgrade.stage_review_bundle

        def keep_hook_local(*args, **kwargs):
            return real(*args, keep_local=(HOOK,), **kwargs)
        with mock.patch.object(upgrade, "stage_review_bundle", side_effect=keep_hook_local):
            staged = self.staged()
        rows = {r["path"]: r["action"] for r in json.loads((staged.bundle / "complete.json").read_text())["plan"]["files"]}
        self.assertEqual(rows[HOOK], "preserve_local")
        before = self.installed()
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "accept-guard-changes"):
            self.apply(staged, choices={"setup.sh": "keep", HOOK: "candidate"})
        self.assertEqual(self.installed(), before)
        applied = self.apply(staged, choices={"setup.sh": "keep"})              # the default, keep, stays free
        self.assertTrue(applied.ok, applied.report)
        self.assertEqual((self.root / HOOK).read_bytes(), b"guard v1 with my own rule\n")

    def test_candidate_on_a_locally_edited_policy_file_needs_the_go_and_both_canaries(self):
        self.edit(POLICY_FILE, b'{"canary": "mine"}\n')
        staged = self.staged()
        self.assertFalse(staged.requires_guard_accept)
        choices = {"setup.sh": "keep", POLICY_FILE: "candidate"}
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "accept-guard-changes"):
            self.apply(staged, choices=choices)
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "new_policy"):
            self.apply(staged, choices=choices, accept_guard_changes=True)
        applied = self.apply(staged, choices=choices, accept_guard_changes=True,
                             new_policy=self.policy(canary_name="new-canary"))
        self.assertTrue(applied.ok, applied.report)
        self.assertEqual(self.names(), ["setup", "new-canary", "canary"])

    def test_keep_on_a_locally_edited_policy_file_needs_neither(self):
        self.edit(POLICY_FILE, b'{"canary": "mine"}\n')
        applied = self.apply(self.staged(), choices={"setup.sh": "keep", POLICY_FILE: "keep"})
        self.assertTrue(applied.ok, applied.report)
        self.assertEqual(self.names(), ["setup", "canary"])


if __name__ == "__main__":
    import unittest
    unittest.main()
