"""Guarded update, end to end: the command line, the exit codes, the session-start line and the vendoring.

Everything goes through `guarded_update.handle` with arguments parsed by the module's own `add_arguments` (next to
`release.add_arguments`, as a toolkit wires it). The fake release is `tests/test_release.py`'s, through its `http`
seam. The apply command is taken from the review text itself, never hand-copied, so the parser and the text the user
is shown cannot drift apart. Setup and canary are tiny real scripts run through `sys.executable`.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

from modding_harness import guarded_update, installation, release
from modding_harness.update_review import UpdatePolicy
from test_release import REPO, FakeGitHub, _no_network, make_zip

HOOK = ".claude/hooks/guard.py"
FAILED = guarded_update.FAILED_MARKER
INFLIGHT = guarded_update.IN_FLIGHT
NEW, OLD = "toolkit-4.3.0/", "toolkit-4.2.0/"
HERE = Path(__file__).resolve().parents[1]

STEP = textwrap.dedent("""\
    import sys
    from pathlib import Path
    hook = Path(".claude/hooks/guard.py").read_bytes()
    broken = sys.argv[1] == "fail" or (sys.argv[1] == "fail-if-v2" and hook == b"guard v2\\n")
    sys.exit(3 if broken else 0)
    """)


def profile():
    """The installed version is read from the installed BUILD-INFO: the only version record there is."""
    return release.ReleaseProfile(REPO, "toolkit-{version}.zip", "the toolkit",
                                  lambda root: release.build_info(root).get("version"))


def parser():
    top = argparse.ArgumentParser()
    commands = top.add_subparsers(dest="command", required=True)
    release.add_arguments(commands)
    guarded_update.add_arguments(commands)
    return top


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "install"
        self.root.mkdir()
        # A folder with a space and parentheses, like the real game folder: the printed command must quote it.
        updates = base / "up dates (x86)"
        for patcher in (mock.patch.object(urllib.request, "urlopen", _no_network),
                        mock.patch.dict(os.environ, {"MODDING_HARNESS_UPDATES_DIR": str(updates)})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.step = base / "step.py"
        self.step.write_text(STEP, encoding="utf-8")
        self.choices = base / "choices.json"
        # The fake release ships the local-notes names too: each is a collision the user decides, and keeps.
        self.choices.write_text(json.dumps({"CLAUDE.local.md": "keep", "KNOWLEDGEBASE.local.md": "keep"}),
                                encoding="utf-8")
        previous = make_zip("4.2.0", commit="c" * 40, extra={OLD + HOOK: b"guard v1\n"})
        with zipfile.ZipFile(io.BytesIO(previous)) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    path = self.root.joinpath(*info.filename.split("/")[1:])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(archive.read(info))
        self.local = {"CLAUDE.local.md": b"my private rules\n", "KNOWLEDGEBASE.local.md": b"my private notes\n"}
        for name, data in self.local.items():
            (self.root / name).write_bytes(data)
        self.previous = previous

    def policy(self, canary="pass"):
        return UpdatePolicy(guard_paths=(".claude/hooks/*",), policy_files=("tools/update_policy.json",),
                            setup=(sys.executable, str(self.step), "pass"),
                            canary=(sys.executable, str(self.step), canary),
                            render_config=lambda root: root / "render.json")

    def github(self, hook=b"guard v1\n"):
        extra = {NEW + HOOK: hook, NEW + "tools/b.py": b"print(2)\n", NEW + "CLAUDE.local.md": b"shipped\n",
                 NEW + "KNOWLEDGEBASE.local.md": b"shipped\n"}
        return FakeGitHub(body=make_zip(extra=extra), previous=self.previous)

    def run_command(self, *argv, policy=None, **kwargs):
        args = parser().parse_args(list(argv))
        return guarded_update.handle(args, self.root, profile(), policy or self.policy(), **kwargs)

    def stage(self, gh, policy=None):
        return self.run_command("update-stage", policy=policy, http=gh)

    def apply_command(self, staged):
        """The exact command the review prints, as argv for the parser, with the placeholder filled in."""
        return apply_argv(staged["review"], self.choices)

    def installed(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*")
                if p.is_file() and ".modding-agent-harness" not in p.relative_to(self.root).parts}


def apply_argv(text, choices):
    """Argv of the `update-apply ...` line printed under 'run exactly:' in a review text."""
    lines = text.splitlines()
    command = next(lines[i + 1] for i, line in enumerate(lines) if line.strip().endswith("run exactly:"))
    argv = shlex.split(command.strip())
    return [str(choices) if part == "<file>" else part for part in argv]


class CommandLineTests(Base):
    def test_commands_are_exactly_the_three_and_do_not_clash_with_release(self):
        self.assertEqual(guarded_update.COMMANDS, frozenset(("update-stage", "update-apply", "update-clear-failed")))
        self.assertFalse(guarded_update.COMMANDS & release.COMMANDS)

    def test_parser_accepts_the_command_the_review_prints(self):
        # The review text, not a hand-copied string: with and without the guard flag, and a bundle path that needs quotes.
        for hook, flagged in ((b"guard v1\n", False), (b"guard v2\n", True)):
            with self.subTest(guard_change=flagged):
                staged = self.stage(self.github(hook))
                argv = self.apply_command(staged)
                self.assertEqual(argv[0], "update-apply")
                self.assertIn("(x86)", argv[argv.index("--bundle") + 1])           # the quoted path survived intact
                args = parser().parse_args(argv)
                self.assertEqual(args.command, "update-apply")
                self.assertEqual(args.bundle, Path(staged["bundle"]))
                self.assertEqual(args.review_sha256, staged["review_sha256"])
                self.assertEqual(args.choices, self.choices)
                self.assertIs(args.accept_guard_changes, flagged)
                self.assertIs(args.quiescent, True)

    def test_update_apply_requires_quiescent_bundle_and_hash(self):
        base = ["update-apply", "--bundle", "b", "--review-sha256", "0" * 64, "--choices", "c"]
        self.assertEqual(parser().parse_args(base + ["--quiescent"]).command, "update-apply")
        for argv in (base, ["update-apply", "--review-sha256", "0" * 64, "--quiescent"],
                     ["update-apply", "--bundle", "b", "--quiescent"]):
            with self.subTest(argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                parser().parse_args(argv)
            self.assertEqual(caught.exception.code, 2)

    def test_handle_passes_quiescent_through_rather_than_assuming_it(self):
        # argparse insists on the flag, but a caller that builds the namespace itself must still be refused.
        staged = self.stage(self.github())
        args = parser().parse_args(self.apply_command(staged))
        args.quiescent = False
        before = self.installed()
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "quiescent") as caught:
            guarded_update.handle(args, self.root, profile(), self.policy())
        self.assertEqual(guarded_update.exit_code(caught.exception), 2)
        self.assertEqual(self.installed(), before)

    def test_an_unreadable_choices_file_is_a_refusal(self):
        staged = self.stage(self.github())
        self.choices.write_text("not json", encoding="utf-8")
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "choices file"):
            self.run_command(*self.apply_command(staged))
        self.choices.unlink()
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "choices file"):
            self.run_command(*self.apply_command(staged))

    def test_handle_refuses_a_command_that_is_not_ours(self):
        args = parser().parse_args(["update-check"])
        with self.assertRaisesRegex(ValueError, "not a guarded update command"):
            guarded_update.handle(args, self.root, profile(), self.policy())


class ExitCodeTests(unittest.TestCase):
    def applied(self, ok):
        return guarded_update.Applied(ok, "a" * 32, not ok, "report")

    def test_cli_exit_codes(self):
        code = guarded_update.exit_code
        self.assertEqual(code(guarded_update.UpdateRefused("no")), 2)               # a refusal
        self.assertEqual(code(OSError("the bundle is gone")), 2)
        self.assertEqual(code(ValueError("not a version")), 2)
        self.assertEqual(code(self.applied(False)), 1)                              # rolled back or partial
        self.assertEqual(code({"ok": False, "operation": "a" * 32}), 1)
        self.assertEqual(code(self.applied(True)), 0)
        self.assertEqual(code({"ok": None}), 1)                                     # only a real True is success
        self.assertEqual(code({"ok": True}), 0)
        self.assertEqual(code({"bundle": "b", "application_authorized": False}), 0)  # stage and clear carry no ok
        self.assertEqual(code(RuntimeError("unexpected")), 1)                       # outcome unknown: not a refusal
        with self.assertRaises(TypeError):
            code(None)
        with self.assertRaises(TypeError):
            code("done")

    def test_exit_code_of_what_handle_really_returns_and_raises(self):
        # The mapping is applied to real results below (see the end-to-end tests); here, the type distinction only.
        self.assertTrue(issubclass(guarded_update.UpdateRefused, ValueError))
        self.assertNotEqual(guarded_update.exit_code(guarded_update.UpdateRefused("x")),
                            guarded_update.exit_code(self.applied(False)))


class EndToEndTests(Base):
    def test_e2e_stage_then_apply_against_fake_release(self):
        gh = self.github()
        before = self.installed()
        staged = self.stage(gh)
        self.assertEqual(self.installed(), before)                                  # staging changed nothing
        self.assertIs(staged["requires_guard_accept"], False)
        self.assertIs(staged["application_authorized"], False)
        self.assertEqual(guarded_update.exit_code(staged), 0)
        self.assertEqual(profile().installed_version(self.root), "4.2.0")
        result = self.run_command(*self.apply_command(staged))
        self.assertIs(result["ok"], True, result["report"])
        self.assertEqual(guarded_update.exit_code(result), 0)
        source = Path(release.updates_home(self.root)) / "4.3.0" / "unpacked"
        shipped = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob("*") if p.is_file()}
        for name, data in shipped.items():
            if name not in self.local:
                with self.subTest(name):
                    self.assertEqual((self.root / name).read_bytes(), data)          # installed bytes equal the release
        self.assertEqual((self.root / "tools/b.py").read_bytes(), b"print(2)\n")    # a file the release added
        for name, data in self.local.items():
            self.assertEqual((self.root / name).read_bytes(), data)                 # local notes untouched
        # No separate inventory or version record: the installed files ARE the record.
        self.assertEqual(profile().installed_version(self.root), "4.3.0")
        state = release.check(self.root, profile(), fetch=gh, force=True)
        self.assertEqual(state["latest"], "4.3.0")
        self.assertFalse(release.newer(state["latest"], profile().installed_version(self.root)))
        self.assertEqual(release.note(self.root, profile(), mark=False), "")
        self.assertIsNone(guarded_update.session_note(self.root))
        self.assertFalse((self.root / INFLIGHT).exists())
        self.assertFalse((self.root / FAILED).exists())
        # Nothing newer is left: staging again refuses, as a refusal (exit 2).
        with self.assertRaises(guarded_update.UpdateRefused) as caught:
            self.stage(gh)
        self.assertEqual(guarded_update.exit_code(caught.exception), 2)

    def test_e2e_guard_change_needs_flag_then_succeeds(self):
        staged = self.stage(self.github(b"guard v2\n"))
        self.assertIs(staged["requires_guard_accept"], True)
        argv = self.apply_command(staged)
        self.assertIn("--accept-guard-changes", argv)
        without = [part for part in argv if part != "--accept-guard-changes"]
        before = self.installed()
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "accept-guard-changes") as caught:
            self.run_command(*without)
        self.assertEqual(guarded_update.exit_code(caught.exception), 2)
        self.assertEqual(self.installed(), before)                                  # nothing changed by the refusal
        self.assertFalse((self.root / INFLIGHT).exists())
        result = self.run_command(*argv)                                            # the second go
        self.assertIs(result["ok"], True, result["report"])
        self.assertEqual((self.root / HOOK).read_bytes(), b"guard v2\n")
        self.assertEqual(profile().installed_version(self.root), "4.3.0")

    def test_e2e_canary_failure_leaves_install_byte_identical(self):
        staged = self.stage(self.github(b"guard v2\n"))
        before = self.installed()
        result = self.run_command(*self.apply_command(staged), policy=self.policy("fail-if-v2"))
        self.assertIs(result["ok"], False)
        self.assertIs(result["rolled_back"], True)
        self.assertEqual(guarded_update.exit_code(result), 1)                       # rolled back is still a failed update
        self.assertEqual(self.installed(), before)                                  # every byte, version included
        self.assertEqual(profile().installed_version(self.root), "4.2.0")
        self.assertIn("rolled back", result["report"])
        self.assertIsNone(guarded_update.session_note(self.root))                   # a clean rollback leaves no marker

    def test_e2e_kill_leaves_the_in_flight_record_and_the_next_stage_exits_2(self):
        staged = self.stage(self.github(b"guard v2\n"))

        def killed(argv, cwd, timeout):
            raise KeyboardInterrupt
        # The interrupt arrives in the first step (setup); the in-flight record is already on disk by then.
        with mock.patch.object(guarded_update, "_default_run", killed), self.assertRaises(KeyboardInterrupt):
            self.run_command(*self.apply_command(staged))
        self.assertTrue((self.root / INFLIGHT).is_file())
        line = guarded_update.session_note(self.root)
        self.assertIn("update-in-flight.json", line)
        self.assertIn("update-clear-failed", line)
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "update-in-flight") as caught:
            self.stage(self.github(b"guard v3\n"))
        self.assertEqual(guarded_update.exit_code(caught.exception), 2)
        cleared = self.run_command("update-clear-failed")
        self.assertEqual(guarded_update.exit_code(cleared), 0)
        self.assertEqual(cleared["cleared"], [INFLIGHT])
        self.assertIsNone(guarded_update.session_note(self.root))
        self.assertEqual(self.run_command("update-clear-failed")["cleared"], [])    # nothing left to clear
        # Staging is no longer blocked by the record. (The interrupted apply had already published 4.3.0, so there is
        # nothing newer: the refusal is now the ordinary one, which also shows the files really were replaced.)
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "no newer release"):
            self.stage(self.github(b"guard v3\n"))

    def test_double_failure_marker_shows_in_the_session_note_until_cleared(self):
        staged = self.stage(self.github(b"guard v2\n"))
        result = self.run_command(*self.apply_command(staged), policy=self.policy("fail"))
        self.assertIs(result["ok"], False)
        self.assertEqual(guarded_update.exit_code(result), 1)
        self.assertIn("update-failed.json", guarded_update.session_note(self.root))
        self.run_command("update-clear-failed")
        self.assertIsNone(guarded_update.session_note(self.root))


class SessionNoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def put(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"v": 1}')
        return path

    def test_session_note_only_while_marker_exists(self):
        self.assertIsNone(guarded_update.session_note(self.root))
        marker = self.put(FAILED)
        line = guarded_update.session_note(self.root)
        self.assertIn("update-failed.json", line)
        self.assertIn("update-clear-failed", line)
        self.assertNotIn("update-in-flight.json", line)
        self.assertNotIn("\n", line)                                                # one line
        marker.unlink()
        self.assertIsNone(guarded_update.session_note(self.root))
        flight = self.put(INFLIGHT)
        line = guarded_update.session_note(self.root)
        self.assertIn("update-in-flight.json", line)
        self.assertIn("update-clear-failed", line)
        self.assertNotIn("update-failed.json", line)
        flight.unlink()
        self.assertIsNone(guarded_update.session_note(self.root))

    def test_session_note_names_both_files_in_one_line_when_both_exist(self):
        self.put(FAILED)
        self.put(INFLIGHT)
        line = guarded_update.session_note(self.root)
        self.assertIn("update-failed.json", line)
        self.assertIn("update-in-flight.json", line)
        self.assertNotIn("\n", line)

    def test_session_note_never_raises_and_never_goes_quiet_on_a_state_it_cannot_read(self):
        self.assertIsNone(guarded_update.session_note(self.root / "no" / "such" / "install"))
        real = os.lstat

        def broken(path, *args, **kwargs):
            if str(path).endswith("update-failed.json"):
                raise PermissionError("denied")
            return real(path, *args, **kwargs)
        with mock.patch.object(guarded_update.os, "lstat", broken):
            line = guarded_update.session_note(self.root)
        self.assertIn("update-failed.json", line)                                   # unknown is not "fine"
        with mock.patch.object(guarded_update.os, "lstat", side_effect=RuntimeError("boom")):
            self.assertIsInstance(guarded_update.session_note(self.root), str)
        self.assertIsInstance(guarded_update.session_note(None), (str, type(None)))


class HookPassThroughTests(Base):
    """`handle` forwards the toolkit's own hooks (`http`, `candidate_preparer`, `generated_paths`, `notes_files`,
    `candidate_validator`, `new_policy`) and nothing else supplies them. Each is checked by a spy that must be reached,
    and the forwarding itself by a wrapper around the call the argument is for."""

    def test_stage_passes_the_toolkit_hooks_through(self):
        config = Path(self.tmp.name) / "render.json"
        config.write_text('{"v": 1, "root": "destination"}')
        policy = UpdatePolicy(guard_paths=(".claude/hooks/*",), policy_files=(), setup=("x",), canary=("x",),
                              render_config=lambda root: config)
        gh, seen = self.github(), []

        def preparer(installed, staged, files, data):
            seen.append((Path(installed), dict(data)))
            return {}
        real = guarded_update.stage_update
        with mock.patch.object(guarded_update, "stage_update", wraps=real) as spy:
            result = self.run_command("update-stage", policy=policy, http=gh, notes_files=("NOTES.md",),
                                      candidate_preparer=preparer, generated_paths=(".codex/hooks.json",))
        kwargs = spy.call_args.kwargs
        self.assertIs(kwargs["candidate_preparer"], preparer)
        self.assertEqual(tuple(kwargs["generated_paths"]), (".codex/hooks.json",))
        self.assertEqual(tuple(kwargs["notes_files"]), ("NOTES.md",))
        self.assertIs(kwargs["http"], gh)
        self.assertEqual(seen, [(self.root.resolve(), {"v": 1, "root": "destination"})])   # the spy was reached
        self.assertTrue(gh.calls)                                                         # the fake http was used
        self.assertEqual(guarded_update.exit_code(result), 0)
        complete = json.loads((Path(result["bundle"]) / "complete.json").read_text(encoding="utf-8"))
        self.assertIn("generated_files", complete)                                        # a renderer ran

    def test_apply_passes_the_toolkit_hooks_through(self):
        # The release adds a policy file, so apply needs the candidate's own policy (`new_policy`).
        gh = self.github()
        gh.body = make_zip(extra={NEW + HOOK: b"guard v1\n", NEW + "tools/update_policy.json": b'{"canary": "B"}\n'})
        gh.digest = "sha256:" + hashlib.sha256(gh.body).hexdigest()
        old = UpdatePolicy(guard_paths=(".claude/hooks/*",), policy_files=("tools/update_policy.json",),
                           setup=(sys.executable, str(self.step), "pass"),
                           canary=(sys.executable, str(self.step), "pass"), render_config=lambda root: root / "x.json")
        (self.root / "NOTES.md").write_bytes(b"toolkit notes the user keeps\n")
        staged = self.run_command("update-stage", policy=old, http=gh, notes_files=("NOTES.md",))
        self.assertIs(staged["requires_guard_accept"], True)
        self.choices.write_text("{}", encoding="utf-8")                                  # this release ships no notes
        argv = apply_argv(staged["review"], self.choices)
        marker = Path(self.tmp.name) / "new-policy-ran"
        new = UpdatePolicy(guard_paths=old.guard_paths, policy_files=old.policy_files,
                           setup=(sys.executable, "-c", "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('x')",
                                  str(marker)),
                           canary=(sys.executable, str(self.step), "pass"), render_config=old.render_config)
        validated = []
        validator = lambda root, changes: validated.append((Path(root), [row["path"] for row in changes]))
        args = parser().parse_args(argv)
        real_apply, real_install = guarded_update.apply_update, installation.apply
        with mock.patch.object(guarded_update, "apply_update", wraps=real_apply) as spy,                 mock.patch.object(installation, "apply", wraps=real_install) as inner:
            result = guarded_update.handle(args, self.root, profile(), old, notes_files=("NOTES.md",),
                                           new_policy=new, candidate_validator=validator)
        self.assertIs(result["ok"], True, result["report"])
        kwargs = spy.call_args.kwargs
        self.assertIs(kwargs["new_policy"], new)
        self.assertIs(kwargs["candidate_validator"], validator)
        self.assertEqual(tuple(kwargs["notes_files"]), ("NOTES.md",))
        self.assertIs(inner.call_args.kwargs["candidate_validator"], validator)
        self.assertIn("NOTES.md", inner.call_args.kwargs["notes_files"])
        self.assertTrue(marker.exists())                                                  # the candidate's setup ran
        self.assertEqual(len(validated), 1)                                               # the validator was reached
        self.assertEqual(validated[0][0], self.root.resolve())
        self.assertIn("tools/update_policy.json", validated[0][1])

    def test_a_changed_policy_file_without_new_policy_is_a_refusal_through_handle(self):
        gh = self.github()
        gh.body = make_zip(extra={NEW + HOOK: b"guard v1\n", NEW + "tools/update_policy.json": b'{"canary": "B"}\n'})
        gh.digest = "sha256:" + hashlib.sha256(gh.body).hexdigest()
        old = UpdatePolicy(guard_paths=(".claude/hooks/*",), policy_files=("tools/update_policy.json",),
                           setup=(sys.executable, str(self.step), "pass"),
                           canary=(sys.executable, str(self.step), "pass"), render_config=lambda root: root / "x.json")
        staged = self.run_command("update-stage", policy=old, http=gh)
        self.choices.write_text("{}", encoding="utf-8")
        before = self.installed()
        with self.assertRaisesRegex(guarded_update.UpdateRefused, "new_policy") as caught:
            self.run_command(*apply_argv(staged["review"], self.choices), policy=old)
        self.assertEqual(guarded_update.exit_code(caught.exception), 2)
        self.assertEqual(self.installed(), before)


class VendoringTests(unittest.TestCase):
    def test_new_modules_are_vendored(self):
        """`scripts/vendor.py` takes every .py under modding_harness from the committed tree (no module list to forget)
        and a vendored copy must import. Run it for real into a scratch toolkit and compare with what is on disk."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "toolkit"
            (target / "tools").mkdir(parents=True)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            done = subprocess.run([sys.executable, str(HERE / "scripts/vendor.py"), str(target)], capture_output=True,
                                  text=True, stdin=subprocess.DEVNULL, creationflags=flags)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            vendored = {p.name for p in (target / "tools/modding_harness").glob("*.py")}
            on_disk = {p.name for p in (HERE / "modding_harness").glob("*.py")}
            self.assertEqual(sorted(on_disk - vendored), [], "a module is not in the vendored set (commit it)")
            for name in ("update_review.py", "guarded_update.py"):
                self.assertIn(name, vendored)
            check = subprocess.run([sys.executable, str(HERE / "scripts/vendor.py"), "--check", str(target)],
                                   capture_output=True, text=True, stdin=subprocess.DEVNULL, creationflags=flags)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            probe = ("import sys; sys.path.insert(0, sys.argv[1]); "
                     "from modding_harness import guarded_update, update_review; "
                     "print('ok')")
            imported = subprocess.run([sys.executable, "-I", "-c", probe, str(target / "tools")], capture_output=True,
                                      text=True, stdin=subprocess.DEVNULL, creationflags=flags)
            self.assertEqual(imported.stdout.strip(), "ok", imported.stderr)


if __name__ == "__main__":
    unittest.main()
