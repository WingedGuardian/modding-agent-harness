import unittest

from modding_harness.transport import execution_path_absolute, normalize


class ExecutionCwdTests(unittest.TestCase):
    def test_windows_targets_need_their_own_drive(self):
        for path in ("/Scratch/a", "\\Scratch\\a", "/c/Scratch/a", "C:Scratch/a", "a", ""):
            self.assertFalse(execution_path_absolute(path, platform="win32"), path)
        for path in ("C:/Scratch/a", "D:\\Saves\\a", "//server/share/a", "\\\\?\\C:\\Saves\\a"):
            self.assertTrue(execution_path_absolute(path, platform="win32"), path)

    def test_posix_targets_keep_their_root_semantics(self):
        self.assertTrue(execution_path_absolute("/scratch/a", platform="linux"))
        self.assertFalse(execution_path_absolute("scratch/a", platform="linux"))
    def test_codex_session_directory_is_not_execution_directory(self):
        for tool in ("Bash", "PowerShell", "exec_command", "shell_command"):
            action = normalize("codex", {"tool_name": tool, "cwd": "C:/Session",
                "tool_input": {"command": "Set-Content Owned.ess x"}},
                platform="win32", default_shell="powershell")
            self.assertIs(action.provenance.get("execution_cwd_known", True), False, tool)
            self.assertEqual(action.cwd, "C:/Session")  # retain the session identity

    def test_absolute_tool_workdir_is_retained(self):
        action = normalize("codex", {"tool_name": "Bash", "cwd": "C:/Session",
            "tool_input": {"command": "Set-Content Owned.ess x", "workdir": "D:/Saves"}},
            platform="win32", default_shell="powershell")
        self.assertEqual(action.cwd, "D:/Saves")
        self.assertIs(action.provenance.get("execution_cwd_known", True), True)

    def test_relative_tool_workdir_does_not_prove_an_execution_directory(self):
        action = normalize("codex", {"tool_name": "Bash", "cwd": "C:/Session",
            "tool_input": {"command": "Set-Content Owned.ess x", "workdir": "Saves"}},
            platform="win32", default_shell="powershell")
        self.assertIs(action.provenance.get("execution_cwd_known", True), False)

    def test_other_hosts_keep_their_cwd_contract(self):
        action = normalize("claude", {"tool_name": "Bash", "cwd": "/project",
            "tool_input": {"command": "echo x > out"}}, platform="linux")
        self.assertIs(action.provenance.get("execution_cwd_known", True), True)
