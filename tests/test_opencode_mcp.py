"""Real Node/Python transport regressions from native V1 MCP discovery."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class OpenCodeMcpTests(unittest.TestCase):
    def drive(self, steps, mode="capture", missing=False):
        node = shutil.which("node")
        if not node:
            if os.environ.get("CI"):
                self.fail("Node is required for OpenCode transport qualification")
            self.skipTest("Node unavailable; process transport not measured")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = root / "adapter.py"
            capture = root / "payloads.jsonl"
            if not missing:
                adapter.write_text(
                    "import json,sys\nfrom pathlib import Path\n"
                    "p=json.load(sys.stdin)\n"
                    "with Path(" + repr(str(capture)) + ").open('a',encoding='utf-8') as f:f.write(json.dumps(p)+'\\n')\n"
                    "mode=" + repr(mode) + "\n"
                    "deny=mode=='policy' and p['tool'].startswith('mcp__devbench')\n"
                    "v={'decision':'deny' if deny else 'advise' if mode=='advice' else 'allow',"
                    "'reason':'known MCP blocked' if deny else None,"
                    "'context':'from:'+p.get('session_id','')+':'+p['tool'] if mode=='advice' else None}\n"
                    "print('HARNESS_OK '+json.dumps(v))\n", encoding="utf-8")
            module = Path(__file__).resolve().parents[1] / "modding_harness/opencode_plugin.mjs"
            driver = root / "driver.mjs"
            driver.write_text(
                "import {createGuard} from " + json.dumps(module.as_uri()) + ";\n"
                "const factory=createGuard({adapter:" + json.dumps(str(adapter)) + ","
                "isJudgedTool:t=>{const p=typeof t==='string'?t.split('__'):[];"
                "return p.length>=3&&p[0]==='mcp'&&p.slice(1).join('__').toLowerCase().replace(/[-_.\\s]/g,'').includes('devbench')}});\n"
                "const instances=new Map();const rows=[];\n"
                "for(const step of " + json.dumps(steps) + "){\n"
                "const id=step.instance??'a';if(!instances.has(id))instances.set(id,await factory({directory:"
                + json.dumps(str(root)) + "}));const hooks=instances.get(id);let error=null;"
                "const output=step.output??{output:'done'};try{\n"
                "if(step.event==='config')await hooks.config(step.config);"
                "else await hooks['tool.execute.'+step.event](step.input,output);"
                "}catch(e){error=e.message}rows.push({error,output});}console.log(JSON.stringify(rows));\n",
                encoding="utf-8")
            env = dict(os.environ, MODDING_HARNESS_PYTHON=sys.executable,
                       MODDING_HARNESS_NO_PYTHON_FALLBACK="1", MODDING_HARNESS_OPENCODE_TIMEOUT_S="2")
            result = subprocess.run([node, str(driver)], capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            payloads = [json.loads(line) for line in capture.read_text(encoding="utf-8").split("\n") if line] if capture.exists() else []
            return json.loads(result.stdout), payloads

    @staticmethod
    def config(servers, instance="a"):
        return {"event": "config", "instance": instance,
                "config": {"mcp": {name: {"type": "local", "command": ["owned-mock"], "enabled": enabled}
                                   for name, enabled in servers.items()}}}

    @staticmethod
    def call(event, tool, session="s", call="c", instance="a"):
        return {"event": event, "instance": instance,
                "input": {"tool": tool, "sessionID": session, "callID": call, "args": {"command": "save Owned"}},
                "output": {"args": {"command": "save Owned"}} if event == "before" else {"output": "done"}}

    def test_native_mcp_alias_reaches_the_profile_policy(self):
        rows, payloads = self.drive([self.config({"devbench_vr": True}),
                                    self.call("before", "devbench_vr_console")], mode="policy")
        self.assertIn("known MCP blocked", rows[-1]["error"] or "")
        self.assertEqual(payloads[0]["tool"], "mcp__devbench_vr__console")
        self.assertEqual(payloads[0]["native_tool"], "devbench_vr_console")
        self.assertEqual(payloads[0]["mcp_identity"]["server"], "devbench_vr")

    def test_mapped_known_tool_refuses_transport_failure_but_unknown_warns_and_allows(self):
        steps = [self.config({"devbench_vr": True, "future": True})]
        for tool in ("devbench_vr_console", "future_tool"):
            steps += [self.call("before", tool, call=tool), self.call("after", tool, call=tool)]
        rows, _ = self.drive(steps, missing=True)
        self.assertIn("GUARD INERT", rows[1]["error"] or "")
        self.assertIsNone(rows[3]["error"])
        self.assertIn("WARN AND ALLOW", rows[4]["output"]["output"])

    def test_disabled_unconfigured_builtin_and_canonical_names_keep_their_identity(self):
        tools = ["devbench_vr_console", "future_console", "write", "mcp__devbench_vr__console"]
        _, payloads = self.drive([self.config({"devbench_vr": False}),
                                 *[self.call("before", tool, call=tool) for tool in tools]])
        self.assertEqual([p["tool"] for p in payloads], tools)
        self.assertTrue(all("mcp_identity" not in p for p in payloads))

    def test_native_sanitizing_keeps_the_configured_server_identity(self):
        server = "my.Dev-Bench__VR"
        _, payloads = self.drive([self.config({server: True}), self.call("before", "my_Dev-Bench__VR_console")])
        self.assertEqual(payloads[0]["tool"], "mcp__my.Dev-Bench__VR__console")
        self.assertEqual(payloads[0]["mcp_identity"]["server"], server)

    def test_mcp_prefixed_server_reaches_exact_profile_on_transport_failure(self):
        for server in ("mcp__devbench_vr", "mcp__devbench__vr"):
            with self.subTest(server=server):
                tool = server + "_console"
                rows, _ = self.drive([self.config({server: True}), self.call("before", tool)], missing=True)
                self.assertIn("GUARD INERT", rows[-1]["error"] or "")

    def test_configured_native_prefix_precedes_canonical_looking_spelling(self):
        for server, suffix in (("mcp__devbench_vr", "console"),
                               ("mcp__devbench__vr", "console"),
                               ("mcp_", "devbench__console"),
                               ("future", "tool__name")):
            with self.subTest(server=server, suffix=suffix):
                native = server + "_" + suffix
                rows, payloads = self.drive([self.config({server: True}), self.call("before", native)])
                self.assertIsNone(rows[-1]["error"])
                self.assertEqual(payloads[0]["tool"], "mcp__" + server + "__" + suffix)
                self.assertEqual(payloads[0]["native_tool"], native)
                self.assertEqual(payloads[0]["mcp_identity"]["server"], server)
                self.assertEqual(payloads[0]["mcp_identity"]["tool"], suffix)

    def test_mcp_prefixed_colliding_servers_refuse_known_and_preserve_unknown(self):
        for servers, native, known in (({"mcp__devbench": True, "mcp__devbench_vr": True},
                                        "mcp__devbench_vr_console", True),
                                       ({"mcp__future": True, "mcp__future_vr": True},
                                        "mcp__future_vr_console", False)):
            with self.subTest(servers=servers):
                rows, payloads = self.drive([self.config(servers), self.call("before", native)])
                if known:
                    self.assertIn("ambiguous", (rows[-1]["error"] or "").lower())
                    self.assertEqual(payloads, [])
                else:
                    self.assertIsNone(rows[-1]["error"])
                    self.assertEqual(payloads[0]["tool"], native)

    def test_builtin_writes_keep_precedence_over_configured_mcp_prefixes(self):
        _, payloads = self.drive([self.config({"apply": True}), self.call("before", "apply_patch")])
        self.assertEqual(payloads[0]["tool"], "apply_patch")
        self.assertNotIn("mcp_identity", payloads[0])

    def test_ambiguous_known_mcp_identity_refuses_without_dispatch(self):
        for servers in ({"devbench": True, "devbench_vr": True}, {"devbench.vr": True, "devbench_vr": True}):
            with self.subTest(servers=servers):
                rows, payloads = self.drive([self.config(servers), self.call("before", "devbench_vr_console")])
                self.assertIn("GUARD INERT", rows[-1]["error"] or "")
                self.assertIn("ambiguous", (rows[-1]["error"] or "").lower())
                self.assertEqual(payloads, [])

    def test_ambiguous_unknown_mcp_keeps_unknown_identity(self):
        rows, payloads = self.drive([self.config({"future": True, "future_vr": True}),
                                    self.call("before", "future_vr_console")])
        self.assertIsNone(rows[-1]["error"])
        self.assertEqual(payloads[0]["tool"], "future_vr_console")

    def test_post_retains_pre_identity_and_advisories_are_session_scoped(self):
        steps = [self.config({"devbench_vr": True}), self.call("before", "devbench_vr_console", session="a"),
                 self.config({}), self.call("before", "devbench_vr_console", session="b"),
                 self.call("after", "devbench_vr_console", session="a"),
                 self.call("after", "devbench_vr_console", session="b")]
        rows, payloads = self.drive(steps, mode="advice")
        self.assertEqual([p["tool"] for p in payloads], ["mcp__devbench_vr__console", "devbench_vr_console",
                                                        "mcp__devbench_vr__console", "devbench_vr_console"])
        self.assertIn("from:a:mcp__devbench_vr__console", rows[4]["output"]["output"])
        self.assertNotIn("from:b:", rows[4]["output"]["output"])
        self.assertIn("from:b:devbench_vr_console", rows[5]["output"]["output"])

    def test_plugin_instances_do_not_share_mcp_configuration(self):
        _, payloads = self.drive([self.config({"devbench_vr": True}, instance="a"), self.config({}, instance="b"),
                                  self.call("before", "devbench_vr_console", instance="a"),
                                  self.call("before", "devbench_vr_console", instance="b")])
        self.assertEqual([p["tool"] for p in payloads], ["mcp__devbench_vr__console", "devbench_vr_console"])

    def test_post_without_pre_does_not_invent_a_qualified_identity(self):
        _, payloads = self.drive([self.config({"devbench_vr": True}), self.call("after", "devbench_vr_console")])
        self.assertEqual(payloads[0]["tool"], "devbench_vr_console")
        self.assertNotIn("mcp_identity", payloads[0])

    def test_a_different_post_tool_cannot_reuse_the_pre_identity_or_notice(self):
        rows, payloads = self.drive([self.config({"devbench_vr": True}),
                                    self.call("before", "devbench_vr_console"),
                                    self.call("after", "future_console")], mode="advice")
        self.assertEqual(payloads[1]["tool"], "future_console")
        self.assertNotIn("mcp_identity", payloads[1])
        self.assertIn("COVERAGE MISMATCH", rows[-1]["output"]["output"])
        self.assertNotIn("from:s:mcp__devbench_vr__console", rows[-1]["output"]["output"])

    def test_mcp_content_retains_advisory_and_original_result_members(self):
        result = {"content": [{"type": "text", "text": "original"},
                              {"type": "resource_link", "uri": "owned://fixture", "name": "fixture"}],
                  "structuredContent": {"owned": True}, "metadata": {"keep": 42}}
        post = self.call("after", "devbench_vr_console")
        post["output"] = result
        for mode in ("advice", "capture"):
            with self.subTest(mode=mode):
                rows, payloads = self.drive([self.config({"devbench_vr": True}),
                                            self.call("before", "devbench_vr_console"), post], mode=mode)
                actual = rows[-1]["output"]
                self.assertEqual(actual["content"][:2], result["content"])
                self.assertEqual(actual["structuredContent"], result["structuredContent"])
                self.assertEqual(actual["metadata"], result["metadata"])
                self.assertNotIn("output", actual)
                if mode == "advice":
                    self.assertEqual(len(actual["content"]), 3)
                    self.assertEqual(actual["content"][-1]["type"], "text")
                    self.assertIn("from:s:mcp__devbench_vr__console", actual["content"][-1]["text"])
                else:
                    self.assertEqual(actual, result)
                self.assertEqual(payloads[-1]["tool"], "mcp__devbench_vr__console")

    def test_unknown_mcp_transport_warning_reaches_native_content_result(self):
        post = self.call("after", "future_console")
        post["output"] = {"content": [{"type": "text", "text": "owned mock completed"}]}
        rows, _ = self.drive([self.config({"future": True}), self.call("before", "future_console"), post], missing=True)
        self.assertIsNone(rows[1]["error"])
        self.assertIsNone(rows[2]["error"])
        self.assertEqual(rows[2]["output"]["content"][0], post["output"]["content"][0])
        self.assertIn("WARN AND ALLOW", rows[2]["output"]["content"][-1]["text"])


if __name__ == "__main__":
    unittest.main()
