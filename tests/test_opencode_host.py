"""The shared OpenCode plugin inside a REAL OpenCode host, driven by a scripted loopback model.

The other OpenCode tests drive the plugin's hooks from node with a fake adapter; they prove the transport, never that
OpenCode calls those hooks and honours a refusal. This one starts the actual `opencode` binary on a throwaway project:
a loopback "model" asks for a bash `rm` of a sentinel file, and the guard adapter refuses it. The same run with an
adapter that allows proves the test can go red: then the sentinel is gone.

No credentials, no network model: the provider is an OpenAI-compatible server on 127.0.0.1 in this process. OpenCode
gets a fresh HOME/XDG folder, so it reads no user configuration. It still installs its provider package from npm at
startup, so the host needs the network once.

Needs MODDING_HARNESS_OPENCODE_BIN (the opencode executable). CI sets MODDING_HARNESS_REQUIRE_OPENCODE_HOST=1, so a
missing binary fails there instead of skipping.
"""
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

MODULE = Path(__file__).resolve().parents[1] / "modding_harness/opencode_plugin.mjs"
MARKER = "GUARD-DENY-SENTINEL"

ADAPTER = r'''
import json, os, sys
event, payload = sys.argv[1], sys.stdin.read()
if event == "pre" and os.environ.get("HOST_TEST_ADAPTER") == "deny" and "GUARD-DENY-SENTINEL" in payload:
    verdict = {"decision": "deny", "reason": "HOST TEST GUARD: refused the sentinel"}
else:
    verdict = {"decision": "allow"}
print("HARNESS_OK " + json.dumps(verdict))
'''


def scripted_model(calls):
    """An OpenAI-compatible chat endpoint: the first request that offers tools gets one tool call, the rest get text."""
    record = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            names = {t.get("function", {}).get("name") for t in body.get("tools") or []}
            done = sum(1 for m in body.get("messages", []) if m.get("role") == "tool")
            record.append([str(m.get("content")) for m in body.get("messages", []) if m.get("role") == "tool"])
            base = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "scripted"}
            if "bash" in names and done < len(calls):
                name, args = calls[done]
                chunks = [{**base, "choices": [{"index": 0, "finish_reason": None, "delta": {"role": "assistant", "tool_calls": [
                              {"index": 0, "id": "call_%d" % done, "type": "function",
                               "function": {"name": name, "arguments": json.dumps(args)}}]}}]},
                          {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}]
            else:
                chunks = [{**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "done"}, "finish_reason": None}]},
                          {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, record


class OpenCodeRealHostTests(unittest.TestCase):
    def setUp(self):
        self.binary = os.environ.get("MODDING_HARNESS_OPENCODE_BIN", "")
        if not self.binary or not Path(self.binary).is_file():
            if os.environ.get("MODDING_HARNESS_REQUIRE_OPENCODE_HOST") == "1":
                self.fail("MODDING_HARNESS_OPENCODE_BIN must name the opencode executable for host qualification")
            self.skipTest("no OpenCode host (set MODDING_HARNESS_OPENCODE_BIN); real-host enforcement was not tested")

    def run_host(self, adapter_mode):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project, home = root / "project", root / "home"
            (project / ".opencode" / "plugins").mkdir(parents=True)
            home.mkdir()
            sentinel = project / (MARKER + ".txt")
            sentinel.write_text("sentinel\n")
            adapter = root / "adapter.py"
            adapter.write_text(ADAPTER)
            (project / ".opencode" / "plugins" / "guard.js").write_text(
                "import { createGuard } from " + json.dumps(MODULE.as_uri()) + "\n"
                "export const HostTestGuard = createGuard({label: 'Host test', adapter: " + json.dumps(str(adapter)) + "})\n")
            server, record = scripted_model([("bash", {"command": 'rm "%s"' % sentinel.as_posix(), "description": "t"})])
            try:
                (project / ".opencode" / "opencode.json").write_text(json.dumps({
                    "$schema": "https://opencode.ai/config.json",
                    "provider": {"scripted": {"npm": "@ai-sdk/openai-compatible", "name": "Scripted",
                                              "options": {"baseURL": "http://127.0.0.1:%d/v1" % server.server_address[1],
                                                          "apiKey": "not-a-key"},
                                              "models": {"m": {"name": "m", "tool_call": True}}}},
                    "model": "scripted/m", "small_model": "scripted/m", "permission": {"bash": "allow"}}))
                subprocess.run(["git", "init", "-q"], cwd=project, check=True)
                keep = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "SYSTEMDRIVE",
                        "PROGRAMFILES", "PROGRAMDATA", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")
                env = {k: v for k, v in os.environ.items() if k.upper() in keep}
                env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "AppData" / "Roaming"),
                           LOCALAPPDATA=str(home / "AppData" / "Local"), XDG_CONFIG_HOME=str(home / ".config"),
                           XDG_DATA_HOME=str(home / ".local" / "share"), XDG_CACHE_HOME=str(home / ".cache"),
                           XDG_STATE_HOME=str(home / ".local" / "state"), MODDING_HARNESS_PYTHON=sys.executable,
                           MODDING_HARNESS_NO_PYTHON_FALLBACK="1", HOST_TEST_ADAPTER=adapter_mode)
                result = subprocess.run([self.binary, "run", "--model", "scripted/m", "go"], cwd=project, env=env,
                                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                                        timeout=300, stdin=subprocess.DEVNULL)
            finally:
                server.shutdown()
                server.server_close()
            return sentinel.exists(), record, result

    def test_a_guard_refusal_stops_the_command_in_a_real_host(self):
        survived, record, result = self.run_host("deny")
        self.assertTrue(record, "the scripted model was never called: " + result.stdout[-2000:] + result.stderr[-2000:])
        self.assertTrue(survived, "the guard refused, but OpenCode ran the rm anyway")
        self.assertTrue(any("HOST TEST GUARD: refused the sentinel" in r for rows in record for r in rows), record)

    def test_the_same_host_runs_the_command_when_the_guard_allows(self):
        """The control: without it, a test whose model call silently failed would pass the first test."""
        survived, record, result = self.run_host("allow")
        self.assertTrue(record, "the scripted model was never called: " + result.stdout[-2000:] + result.stderr[-2000:])
        self.assertFalse(survived, "the guard allowed, but the rm did not run: the host test cannot go red")


if __name__ == "__main__":
    unittest.main()
