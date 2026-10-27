"""Dynamic same-instance MCP identity regressions grounded in native V1 probes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class OpenCodeMcpLifecycleTests(unittest.TestCase):
    def drive(self, steps, *, missing=False, fake_clock=False, no_client=False):
        node = shutil.which("node")
        if not node:
            if os.environ.get("CI"):
                self.fail("Node is required for native transport qualification")
            self.skipTest("Node unavailable; MCP lifecycle not measured")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "payloads.jsonl"
            adapter = root / "adapter.py"
            if not missing:
                adapter.write_text(
                    "import json,sys\nfrom pathlib import Path\n"
                    "p=json.load(sys.stdin)\n"
                    "with Path(" + repr(str(capture)) + ").open('a',encoding='utf-8') as f:f.write(json.dumps(p)+'\\n')\n"
                    "print('HARNESS_OK '+json.dumps({'decision':'allow'}))\n", encoding="utf-8")
            module = Path(__file__).resolve().parents[1] / "modding_harness/opencode_plugin.mjs"
            driver = root / "driver.mjs"
            source = r'''
import {createGuard} from MODULE;
let clock=10000;const timers=[];const realTimer=globalThis.setTimeout;
if(FAKECLOCK){
 Object.defineProperty(globalThis,'performance',{value:{now:()=>clock},configurable:true});
 globalThis.setTimeout=(fn,ms,...args)=>{timers.push(ms);return realTimer(fn,ms,...args)};
}
const instances=new Map(),calls=[],aborted=[],rows=[];
const factory=createGuard({adapter:ADAPTER,isJudgedTool:t=>{
 const p=typeof t==='string'?t.split('__'):[];
 return p.length>=3&&p[0]==='mcp'&&p.slice(1).join('__').toLowerCase().replace(/[-_.\s]/g,'').includes('devbench');
}});
for(const step of STEPS){
 const id=step.instance??'a';
 if(!instances.has(id)){
  const control={reply:{data:{}},error:false,hang:false,advance:0};
  const client={mcp:{status:async opts=>{
   calls.push({instance:id,directory:opts?.query?.directory,signal:!!opts?.signal,
    throwOnError:opts?.throwOnError,responseStyle:opts?.responseStyle});
   clock+=control.advance;
   if(control.error)throw Error('synthetic-private-detail-never-echo');
   if(control.hang)return await new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>{
    aborted.push(id);reject(Error('owned request aborted'));
   },{once:true}));
   return control.reply;
  }}};
  instances.set(id,{control,hooks:await factory({directory:ROOT+(id==='a'?'':'/'+id),...(NOCLIENT?{}:{client})})});
 }
 const {control,hooks}=instances.get(id);let error=null;const output=step.output??{output:'original'};
 try{
  if(step.event==='catalog'){control.reply=step.reply;control.error=!!step.error;control.hang=!!step.hang;control.advance=step.advance??0}
  else if(step.event==='config')await hooks.config(step.config);
  else await hooks['tool.execute.'+step.event](step.input,output);
 }catch(e){error=e.message}
 rows.push({error,output});
}
console.log(JSON.stringify({rows,calls,aborted,timers}));
'''
            for marker, value in {"MODULE": module.as_uri(), "ADAPTER": str(adapter),
                                  "ROOT": str(root), "STEPS": steps, "FAKECLOCK": fake_clock,
                                  "NOCLIENT": no_client}.items():
                source = source.replace(marker, json.dumps(value))
            driver.write_text(source, encoding="utf-8")
            env = dict(os.environ, MODDING_HARNESS_PYTHON=sys.executable,
                       MODDING_HARNESS_NO_PYTHON_FALLBACK="1", MODDING_HARNESS_OPENCODE_TIMEOUT_S="2")
            result = subprocess.run([node, str(driver)], capture_output=True, text=True, env=env, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            payloads = [json.loads(line) for line in capture.read_text(encoding="utf-8").split("\n") if line] if capture.exists() else []
            return json.loads(result.stdout), payloads

    @staticmethod
    def config(servers, instance="a"):
        return {"event": "config", "instance": instance,
                "config": {"mcp": {name: {"type": "local", "enabled": enabled} for name, enabled in servers.items()}}}

    @staticmethod
    def catalog(servers=None, *, instance="a", **kwargs):
        return {"event": "catalog", "instance": instance,
                "reply": kwargs.pop("reply", {"data": {name: {"status": status} for name, status in (servers or {}).items()}}), **kwargs}

    @staticmethod
    def call(event, tool="devbench_vr_console", *, instance="a", call="c", session="s"):
        return {"event": event, "instance": instance,
                "input": {"tool": tool, "sessionID": session, "callID": call, "args": {"command": "save Owned"}},
                "output": {"args": {"command": "save Owned"}} if event == "before" else {"content": [{"type": "text", "text": "original"}]}}

    def test_runtime_added_server_reaches_exact_profile_and_native_provenance(self):
        result, payloads = self.drive([self.config({}), self.catalog({"devbench_vr": "connected"}), self.call("before")])
        self.assertIsNone(result["rows"][-1]["error"])
        self.assertEqual(payloads[0]["tool"], "mcp__devbench_vr__console")
        self.assertEqual(payloads[0]["native_tool"], "devbench_vr_console")
        self.assertEqual(payloads[0]["mcp_identity"]["server"], "devbench_vr")
        self.assertEqual(len(result["calls"]), 1)
        query = result["calls"][0]
        self.assertTrue(query["signal"] and query["throwOnError"])
        self.assertEqual(query["responseStyle"], "fields")
        self.assertEqual(query["directory"], payloads[0]["directory"])

    def test_initially_disabled_server_can_be_connected_without_config_refresh(self):
        _, payloads = self.drive([self.config({"devbench_vr": False}), self.catalog({"devbench_vr": "connected"}), self.call("before")])
        self.assertEqual(payloads[0]["tool"], "mcp__devbench_vr__console")

    def test_dynamic_canonical_looking_server_uses_authoritative_prefix(self):
        for server in ("mcp__devbench_vr", "mcp__devbench__vr", "my.Dev-Bench__VR", "Dev Bench VR"):
            with self.subTest(server=server):
                native = server.replace(".", "_").replace(" ", "_") + "_console"
                _, payloads = self.drive([self.config({}), self.catalog({server: "connected"}), self.call("before", native)])
                self.assertEqual(payloads[0]["tool"], "mcp__" + server + "__console")
                self.assertEqual(payloads[0]["mcp_identity"]["server"], server)

    def test_each_pre_uses_current_catalog_without_static_reconfiguration(self):
        result, payloads = self.drive([self.config({}), self.catalog({}), self.call("before", call="a"),
                                      self.catalog({"devbench_vr": "connected"}), self.call("before", call="b"),
                                      self.catalog({}), self.call("before", call="c")])
        self.assertEqual([p["tool"] for p in payloads], ["devbench_vr_console", "mcp__devbench_vr__console", "devbench_vr_console"])
        self.assertEqual(len(result["calls"]), 3)

    def test_configured_and_live_same_server_is_one_identity(self):
        result, payloads = self.drive([self.config({"devbench_vr": True}), self.catalog({"devbench_vr": "connected"}), self.call("before")])
        self.assertIsNone(result["rows"][-1]["error"])
        self.assertEqual(payloads[0]["tool"], "mcp__devbench_vr__console")
        self.assertEqual(len(result["calls"]), 1)

    def test_live_prefix_or_sanitizing_ambiguity_refuses_known_and_retains_unknown(self):
        for servers, native, known in ((["devbench", "devbench_vr"], "devbench_vr_console", True),
                                        (["devbench.vr", "devbench_vr"], "devbench_vr_console", True),
                                        (["mcp__devbench", "mcp__devbench_vr"], "mcp__devbench_vr_console", True),
                                        (["future", "future_vr"], "future_vr_console", False)):
            with self.subTest(servers=servers):
                result, payloads = self.drive([self.config({}), self.catalog({s: "connected" for s in servers}), self.call("before", native)])
                if known:
                    self.assertIn("ambiguous", result["rows"][-1]["error"] or "")
                    self.assertEqual(payloads, [])
                else:
                    self.assertIsNone(result["rows"][-1]["error"])
                    self.assertEqual(payloads[0]["tool"], native)

    def test_only_connected_dynamic_servers_supply_live_aliases(self):
        servers = {"disabled": "disabled", "failed": "failed", "auth": "needs_auth", "registration": "needs_client_registration", "future": "connected"}
        _, payloads = self.drive([self.config({}), self.catalog(servers),
                                 *[self.call("before", name + "_console", call=name) for name in servers]])
        self.assertEqual([p["tool"] for p in payloads], ["disabled_console", "failed_console", "auth_console", "registration_console", "mcp__future__console"])

    def test_dynamic_known_missing_adapter_refuses_while_unknown_warning_reaches_content(self):
        result, payloads = self.drive([self.config({}), self.catalog({"devbench_vr": "connected", "future": "connected"}),
                                      self.call("before"), self.call("before", "future_console", call="u"),
                                      self.call("after", "future_console", call="u")], missing=True)
        self.assertIn("GUARD INERT", result["rows"][2]["error"] or "")
        self.assertIsNone(result["rows"][3]["error"])
        self.assertIn("WARN AND ALLOW", result["rows"][-1]["output"]["content"][-1]["text"])
        self.assertEqual(payloads, [])

    def test_unavailable_catalog_refuses_known_configured_disabled_and_canonical_routes(self):
        for enabled, native in ((True, "devbench_vr_console"), (False, "devbench_vr_console"), (False, "mcp__devbench_vr__console")):
            with self.subTest(enabled=enabled, native=native):
                result, payloads = self.drive([self.config({"devbench_vr": enabled}), self.catalog(error=True), self.call("before", native)])
                self.assertIn("GUARD INERT", result["rows"][-1]["error"] or "")
                self.assertIn("catalog", (result["rows"][-1]["error"] or "").lower())
                self.assertNotIn("synthetic-private-detail", result["rows"][-1]["error"] or "")
                self.assertEqual(payloads, [])

    def test_unavailable_catalog_refuses_a_previously_discovered_known_route(self):
        result, payloads = self.drive([self.config({}), self.catalog({"devbench_vr": "connected"}), self.call("before", call="a"),
                                      self.catalog(error=True), self.call("before", call="b")])
        self.assertEqual(payloads[0]["tool"], "mcp__devbench_vr__console")
        self.assertEqual(len(payloads), 1)
        self.assertIn("GUARD INERT", result["rows"][-1]["error"] or "")

    def test_unknown_catalog_failure_is_sparse_visible_and_does_not_echo_error(self):
        steps = [self.config({}), self.catalog(error=True)]
        for number in range(1, 5):
            steps += [self.call("before", "future_console", call=str(number)), self.call("after", "future_console", call=str(number))]
        result, payloads = self.drive(steps)
        self.assertTrue(all(row["error"] is None for row in result["rows"]))
        outputs = [result["rows"][index]["output"]["content"] for index in (3, 5, 7, 9)]
        self.assertEqual([any("MCP CATALOG UNAVAILABLE" in x["text"] for x in output if x["type"] == "text") for output in outputs], [True, True, False, True])
        self.assertNotIn("synthetic-private-detail", json.dumps(result["rows"]))
        self.assertEqual(len(payloads), 8)

    def test_unknown_catalog_and_adapter_failures_keep_both_disclosures(self):
        result, _ = self.drive([self.config({}), self.catalog(error=True), self.call("before", "future_console"), self.call("after", "future_console")], missing=True)
        text = result["rows"][-1]["output"]["content"][-1]["text"]
        self.assertIn("MCP CATALOG UNAVAILABLE", text)
        self.assertIn("coverage receipt unavailable", text)
        self.assertIsNone(result["rows"][2]["error"])

    def test_hanging_catalog_is_aborted_and_known_call_never_dispatches(self):
        result, payloads = self.drive([self.config({"devbench_vr": True}), self.catalog(hang=True), self.call("before")])
        self.assertIn("GUARD INERT", result["rows"][-1]["error"] or "")
        self.assertEqual(result["aborted"], ["a"])
        self.assertEqual(payloads, [])

    def test_malformed_or_error_catalog_cannot_supply_partial_identity(self):
        replies = [None, {"data": []}, {"data": {"devbench_vr": "connected"}},
                   {"data": {"devbench_vr": {"status": "connected"}, "broken": None}},
                   {"data": {"devbench_vr": {"status": "new-unmeasured-state"}}},
                   {"data": {"devbench_vr": {"status": "connected"}}, "error": {"detail": "private"}}]
        for reply in replies:
            with self.subTest(reply=reply):
                result, payloads = self.drive([self.config({"devbench_vr": False}), self.catalog(reply=reply), self.call("before")])
                self.assertIn("GUARD INERT", result["rows"][-1]["error"] or "")
                self.assertEqual(payloads, [])

    def test_runtime_catalogs_and_failure_knowledge_are_instance_scoped(self):
        result, payloads = self.drive([self.config({}, "a"), self.config({}, "b"),
                                      self.catalog({"devbench_vr": "connected"}, instance="a"),
                                      self.call("before", instance="a"), self.call("before", instance="b"),
                                      self.catalog(error=True, instance="b"), self.call("before", instance="b", call="b")])
        self.assertEqual([p["tool"] for p in payloads], ["mcp__devbench_vr__console", "devbench_vr_console", "devbench_vr_console"])
        self.assertIsNone(result["rows"][-1]["error"])
        self.assertNotEqual(result["calls"][0]["directory"], result["calls"][1]["directory"])

    def test_post_uses_pre_identity_without_a_second_live_lookup(self):
        result, payloads = self.drive([self.config({}), self.catalog({"devbench_vr": "connected"}), self.call("before"),
                                      self.catalog(error=True), self.call("after")])
        self.assertEqual([p["tool"] for p in payloads], ["mcp__devbench_vr__console", "mcp__devbench_vr__console"])
        self.assertEqual(len(result["calls"]), 1)
        self.assertTrue(all(row["error"] is None for row in result["rows"]))

    def test_builtin_priority_does_not_depend_on_mcp_catalog_availability(self):
        result, payloads = self.drive([self.config({"apply": True}), self.catalog(error=True), self.call("before", "apply_patch")])
        self.assertEqual(result["calls"], [])
        self.assertIsNone(result["rows"][-1]["error"])
        self.assertEqual(payloads[0]["tool"], "apply_patch")

    def test_catalog_time_is_charged_to_the_same_adapter_budget(self):
        result, payloads = self.drive([self.config({}), self.catalog({"devbench_vr": "connected"}, advance=900), self.call("before")], fake_clock=True)
        self.assertIsNone(result["rows"][-1]["error"])
        self.assertEqual(result["timers"], [1000, 600])
        self.assertEqual(payloads[0]["tool"], "mcp__devbench_vr__console")

    def test_host_without_status_api_retains_the_static_contract(self):
        result, payloads = self.drive([self.config({"devbench_vr": True}), self.call("before")], no_client=True)
        self.assertIsNone(result["rows"][-1]["error"])
        self.assertEqual(result["calls"], [])
        self.assertEqual(payloads[0]["tool"], "mcp__devbench_vr__console")

    def test_unknown_catalog_notice_cannot_attach_to_a_different_post_tool(self):
        result, payloads = self.drive([self.config({}), self.catalog(error=True),
                                      self.call("before", "future_console"),
                                      self.call("after", "different_console")])
        text = result["rows"][-1]["output"]["content"][-1]["text"]
        self.assertIn("COVERAGE MISMATCH", text)
        self.assertNotIn("MCP CATALOG UNAVAILABLE", text)
        self.assertEqual(payloads[-1]["tool"], "different_console")

    def test_unidentified_first_dynamic_call_has_explicit_unknown_fallback_on_catalog_failure(self):
        result, payloads = self.drive([self.config({}), self.catalog(error=True),
                                      self.call("before"), self.call("after")])
        self.assertTrue(all(row["error"] is None for row in result["rows"]))
        self.assertEqual([p["tool"] for p in payloads], ["devbench_vr_console", "devbench_vr_console"])
        self.assertTrue(all("mcp_identity" not in p for p in payloads))
        text = result["rows"][-1]["output"]["content"][-1]["text"]
        self.assertIn("MCP CATALOG UNAVAILABLE", text)
        self.assertIn("native MCP policy was not established", text)
        self.assertEqual(len(result["calls"]), 1)



if __name__ == "__main__":
    unittest.main()
