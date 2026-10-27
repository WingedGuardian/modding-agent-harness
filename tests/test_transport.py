import unittest

from modding_harness.transport import BadPayload, load, normalize


class TransportTests(unittest.TestCase):
    def test_hosts_keep_their_native_patch_grammar_and_full_path_mapping(self):
        text = "*** Begin Patch\n*** Add File: a\n+x\n  *** Delete File: b\n*** End Patch"
        codex = normalize("codex", {"tool_name": "apply_patch", "tool_input": {"input": text}, "cwd": "/workspace"}, platform="linux")
        opencode = normalize("opencode", {"v": 1, "tool": "apply_patch", "args": {"patchText": text}, "directory": "/workspace"}, platform="linux")
        self.assertEqual((codex.paths, codex.operations), (("/workspace/a", "/workspace/b"), ("write", "delete")))
        self.assertEqual(opencode.paths, ("/workspace/a",))

    def test_actual_workdir_shell_and_session_identity_survive(self):
        payload = {"v": 1, "tool": "bash", "args": {"command": "echo fixture", "workdir": "Sub"},
                   "directory": "C:/Workspace", "shell": "powershell", "session_id": "s", "call_id": "c"}
        action = normalize("opencode", payload, platform="win32")
        self.assertEqual((action.cwd, action.shell, action.session_id, action.tool_call_id),
                         ("C:/Workspace/Sub", "powershell", "s", "c"))
        self.assertEqual(action.provenance["raw_payload"], payload)

    def test_unknown_warn_policy_is_left_to_dispatcher_and_stdin_is_a_gap(self):
        action = normalize("claude", {"tool_name": "future", "tool_input": {"command": "a recipe"}})
        self.assertEqual(action.surface, "unknown")
        action = normalize("codex", {"tool_name": "write_stdin", "tool_input": {"chars": "x\n"}, "cwd": "/work"}, platform="linux")
        self.assertEqual(action.surface, "stdin")
        self.assertIn("no qualified", action.provenance["coverage"])

    def test_malformed_known_and_duplicate_payloads_refuse(self):
        for payload in ({"tool_name": "Write", "tool_input": {}, "cwd": "/work"},
                        {"tool_name": "Bash", "tool_input": {"command": "x"}, "cwd": "/work"},
                        {"tool_name": "Bash", "tool_input": {"cmd": "x", "command": "y"}, "cwd": "/work"}):
            with self.assertRaises(BadPayload):
                normalize("codex", payload, platform="linux")
        with self.assertRaises(BadPayload):
            load('{"tool_name":"x","tool_name":"y"}')


if __name__ == "__main__":
    unittest.main()
