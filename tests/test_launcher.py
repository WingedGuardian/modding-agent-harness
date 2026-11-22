"""The launcher starts any coding agent from the install folder, whatever folder the terminal opened in.

An agent loads its hooks and instructions from the folder it is started in, so a session begun elsewhere runs with no
guards at all. The launcher is a tiny script that changes into the install folder and then starts the chosen agent.
It is agent-neutral (the toolkit names the agents; `-- <command>` starts any other), edits no PATH and no shell profile,
and quotes the install path for the shell it is written for.
"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from modding_harness import launcher

AGENTS = {"alpha": "fakeagent", "beta": "otheragent"}
AWKWARD = "X4 Foundations (x86) it's [a]$b&c"        # spaces, parentheses, apostrophe, brackets, dollar, ampersand


def _workspace(root, name=AWKWARD):
    path = Path(root) / name
    path.mkdir()
    return path


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = _workspace(self.tmp.name)

    def test_bash_quotes_the_path_so_the_shell_cannot_reinterpret_it(self):
        text = launcher.render(self.ws, "bash", AGENTS)
        self.assertIn("ROOT='" + str(self.ws).replace("'", "'\\''") + "'", text)
        self.assertNotIn('ROOT="', text)                      # never a double-quoted path: $ and backticks would expand
        self.assertTrue(text.startswith("#!/usr/bin/env bash\n"))
        self.assertNotIn("\r", text)

    def test_powershell_doubles_every_quote_character_powershell_treats_as_a_quote(self):
        smart = Path(self.tmp.name) / "it’s a ‘test’ ‚ ‛"      # PowerShell reads these as quotes too
        smart.mkdir()
        text = launcher.render(smart, "powershell", AGENTS)
        line = next(l for l in text.splitlines() if l.startswith("$Root = "))
        body = line[len("$Root = '"):-1]
        for quote in ("'", "‘", "’", "‚", "‛"):
            if quote in str(smart):
                # each occurrence in the path appears doubled in the literal, and no single (undoubled) one is left
                self.assertEqual(body.count(quote + quote), str(smart).count(quote), quote)
                self.assertEqual(body.count(quote), 2 * str(smart).count(quote), quote)
        self.assertIn("Set-Location -LiteralPath $Root", text)

    def test_cmd_wrapper_never_embeds_the_path(self):
        text = launcher.render(self.ws, "cmd", AGENTS, name="agent")
        self.assertNotIn(str(self.ws), text)                  # cmd quoting of % ^ & is not worth the risk: it delegates
        self.assertIn('%~dp0agent.ps1', text)
        self.assertIn("-NoProfile", text)
        self.assertNotIn("Bypass", text)                      # never loosens the user's execution policy
        self.assertTrue(text.endswith("\r\n") and "\n" not in text.replace("\r\n", ""))

    def test_every_agent_and_the_any_command_form_are_listed(self):
        for shell in ("bash", "powershell"):
            text = launcher.render(self.ws, shell, AGENTS)
            for name, command in AGENTS.items():
                self.assertIn(name, text)
                self.assertIn(command, text)
            self.assertIn("--", text)

    def test_a_missing_folder_or_odd_characters_are_refused_not_written(self):
        with self.assertRaises(ValueError):
            launcher.render(Path(self.tmp.name) / "absent", "bash", AGENTS)
        for bad in ("a\nb", "a\rb", "a\x00b"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                launcher.quote_path(bad, "bash")

    def test_agent_names_and_commands_must_be_plain_tokens(self):
        for bad in ({"a b": "x"}, {"a;b": "x"}, {"a": "x y"}, {"a": "$(x)"}, {"a": ""}, {"": "x"}, {"-x": "y"}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                launcher.render(self.ws, "bash", bad)

    def test_unknown_shell_is_refused(self):
        with self.assertRaises(ValueError):
            launcher.render(self.ws, "zsh", AGENTS)


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = _workspace(self.tmp.name)
        self.out = Path(self.tmp.name) / "bin"

    def test_writes_exactly_the_requested_files_and_nothing_else(self):
        written = launcher.write(self.ws, self.out, AGENTS, name="x4")
        self.assertEqual(sorted(p.name for p in written), ["x4", "x4.cmd", "x4.ps1"])
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["x4", "x4.cmd", "x4.ps1"])
        self.assertFalse(any(self.out.parent.glob("*.bak")))

    def test_line_endings_are_written_as_bytes_not_translated(self):
        launcher.write(self.ws, self.out, AGENTS, name="x4")
        self.assertNotIn(b"\r", (self.out / "x4").read_bytes())
        self.assertIn(b"\r\n", (self.out / "x4.cmd").read_bytes())
        self.assertIn(b"\r\n", (self.out / "x4.ps1").read_bytes())

    def test_refuses_to_overwrite_a_file_it_did_not_write(self):
        self.out.mkdir()
        (self.out / "x4").write_text("my own script\n")
        with self.assertRaises(FileExistsError):
            launcher.write(self.ws, self.out, AGENTS, name="x4")
        self.assertEqual((self.out / "x4").read_text(), "my own script\n")
        self.assertFalse((self.out / "x4.cmd").exists())      # nothing partial is left behind

    def test_rewrites_its_own_files_after_a_move(self):
        launcher.write(self.ws, self.out, AGENTS, name="x4")
        moved = _workspace(self.tmp.name, "elsewhere")
        launcher.write(moved, self.out, AGENTS, name="x4")
        self.assertIn("elsewhere", (self.out / "x4").read_text())

    def test_name_cannot_escape_the_output_folder(self):
        for bad in ("../x4", "a/b", "a\\b", "", ".", "x4.cmd", "con"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                launcher.write(self.ws, self.out, AGENTS, name=bad)
        self.assertFalse(self.out.exists())


def _working_bash():
    """A bash that really runs scripts (on Windows, System32's bash.exe is the WSL stub and fails without a distro)."""
    exe = shutil.which("bash")
    if not exe:
        return None
    try:
        done = subprocess.run([exe, "-c", "exit 0"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return exe if done.returncode == 0 else None


BASH = _working_bash()


@unittest.skipUnless(BASH, "no working bash on PATH: the generated bash script was only checked structurally")
class BashBehaviourTests(unittest.TestCase):
    """Runs the generated script for real against a stub 'agent' that only prints where it was started."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = _workspace(self.tmp.name)
        self.bin = Path(self.tmp.name) / "stubs"
        self.bin.mkdir()
        stub = self.bin / "fakeagent"
        stub.write_bytes(b'#!/usr/bin/env bash\nprintf "cwd=%s args=%s\\n" "$(pwd)" "$*"\n')
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        self.out = Path(self.tmp.name) / "launchers"
        launcher.write(self.ws, self.out, AGENTS, name="x4")
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""))

    def _run(self, *args, cwd=None):
        return subprocess.run([BASH, str(self.out / "x4"), *args], cwd=cwd or self.tmp.name,
                              env=self.env, capture_output=True, text=True, timeout=60)

    def test_starts_the_agent_in_the_install_folder_from_anywhere(self):
        done = self._run("alpha", "one two", cwd=self.tmp.name)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("args=one two", done.stdout)
        self.assertEqual(os.path.basename(done.stdout.split("cwd=")[1].split(" args=")[0]), AWKWARD)

    def test_any_other_command_runs_after_double_dash(self):
        done = self._run("--", "fakeagent", "x")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("args=x", done.stdout)

    def test_no_argument_lists_the_agents_and_exits_cleanly(self):
        done = self._run()
        self.assertEqual(done.returncode, 0)
        self.assertIn("alpha", done.stdout)
        self.assertIn("beta", done.stdout)

    def test_unknown_agent_is_refused_with_a_nonzero_exit(self):
        done = self._run("gamma")
        self.assertEqual(done.returncode, 2)
        self.assertIn("gamma", done.stderr)

    def test_a_vanished_install_folder_is_a_clear_refusal_not_a_start_in_the_wrong_place(self):
        shutil.rmtree(self.ws)
        done = self._run("alpha")
        self.assertEqual(done.returncode, 2)
        self.assertNotIn("cwd=", done.stdout)
        self.assertIn("not found", done.stderr)


def _working_powershell():
    for candidate in ("pwsh", "powershell"):
        exe = shutil.which(candidate)
        if not exe:
            continue
        try:
            done = subprocess.run([exe, "-NoProfile", "-Command", "exit 0"], capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            return exe
    return None


POWERSHELL = _working_powershell()


@unittest.skipUnless(POWERSHELL, "no working PowerShell on PATH: the generated .ps1 was only checked structurally")
class PowerShellBehaviourTests(unittest.TestCase):
    """Runs the generated .ps1 for real. The agent is PowerShell itself, started through the `--` form, printing its folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = _workspace(self.tmp.name, "it’s a X4 (x86) [a]$b&c")      # includes a smart apostrophe
        self.out = Path(self.tmp.name) / "launchers"
        launcher.write(self.ws, self.out, AGENTS, name="x4")

    def _run(self, *args):
        return subprocess.run([POWERSHELL, "-NoProfile", "-File", str(self.out / "x4.ps1"), *args], cwd=self.tmp.name,
                              capture_output=True, text=True, encoding="utf-8", timeout=120)

    def test_starts_the_command_in_the_install_folder_from_anywhere(self):
        # The child writes UTF-8 explicitly: a console code page would turn the curly apostrophe into a plain one and
        # the comparison would measure the console, not the launcher.
        done = self._run("--", POWERSHELL, "-NoProfile", "-Command",
                         "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; (Get-Location).ProviderPath")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), str(self.ws.resolve()))

    def test_unknown_agent_exits_2_and_a_vanished_folder_starts_nothing(self):
        self.assertEqual(self._run("gamma").returncode, 2)
        shutil.rmtree(self.ws)
        done = self._run("--", POWERSHELL, "-NoProfile", "-Command", "'STARTED'")
        self.assertEqual(done.returncode, 2)
        self.assertNotIn("STARTED", done.stdout)

    def test_no_argument_lists_the_agents(self):
        done = self._run()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("alpha", done.stdout)


if __name__ == "__main__":
    unittest.main()
