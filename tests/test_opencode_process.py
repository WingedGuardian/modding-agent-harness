"""Process-level transport controls without installing or trusting a host."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


class OpenCodeProcessTests(unittest.TestCase):
    def test_profile_known_tools_refuse_transport_failure_while_unknowns_warn_and_allow(self):
        node = shutil.which("node")
        if not node:
            if os.environ.get("CI"):
                self.fail("Node is required for process transport qualification")
            self.skipTest("Node unavailable; process transport was not tested")
        module = Path(__file__).resolve().parents[1] / "modding_harness/opencode_plugin.mjs"
        for failure in ("no-python", "no-adapter", "nonzero", "bad-json", "timeout", "healthy"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                adapter = root / "adapter.py"
                body = {"nonzero": "print('HARNESS_OK {\\\"decision\\\":\\\"allow\\\"}'); raise SystemExit(7)\n",
                        "bad-json": "print('HARNESS_OK broken')\n", "timeout": "import time; time.sleep(5)\n"}
                adapter.write_text(body.get(failure, "print('HARNESS_OK {\\\"decision\\\":\\\"allow\\\"}')\n"))
                if failure == "no-adapter":
                    adapter.unlink()
                driver = root / "driver.mjs"
                driver.write_text("import { createGuard } from " + json.dumps(module.as_uri()) + ";\n" +
                    "const hooks=await createGuard({adapter:" + json.dumps(str(adapter)) +
                    ",isJudgedTool:tool=>tool.startsWith('mcp__qualified__')})({directory:" + json.dumps(str(root)) + "});\n" +
                    "const rows=[]; for(const tool of ['write','mcp__qualified__save','mcp__future__tool','future-tool']) {\n"
                    " const input={tool,sessionID:'s',callID:tool}; let error=null;\n"
                    " try {await hooks['tool.execute.before'](input,{args:{filePath:'a'}})} catch(e){error=e.message}\n"
                    " const output={output:'done'}; await hooks['tool.execute.after']({...input,args:{}},output);\n"
                    " rows.push({tool,error,output:output.output}); } console.log(JSON.stringify(rows));\n")
                env = dict(os.environ, MODDING_HARNESS_PYTHON=(str(root / "missing-python") if failure == "no-python" else sys.executable),
                           MODDING_HARNESS_NO_PYTHON_FALLBACK="1", MODDING_HARNESS_OPENCODE_TIMEOUT_S="0.8")
                result = subprocess.run([node, str(driver)], capture_output=True, text=True, env=env, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                rows = json.loads(result.stdout)
                if failure == "healthy":
                    self.assertTrue(all(row["error"] is None for row in rows), rows)
                else:
                    for row in rows[:2]:
                        self.assertIn("GUARD INERT", row["error"] or "", row)
                        self.assertNotIn("WARN AND ALLOW", row["output"])
                    for row in rows[2:]:
                        self.assertIsNone(row["error"], row)
                        self.assertIn("WARN AND ALLOW", row["output"])
                        self.assertIn("coverage receipt unavailable", row["output"])

    def test_python_discovery_is_inside_the_total_transport_budget(self):
        node = shutil.which("node")
        if not node:
            if os.environ.get("CI"):
                self.fail("Node is required for process transport qualification")
            self.skipTest("Node unavailable; process transport was not tested")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # Python's own startup is delayed before the version probe completes.
            # There is no model, OpenCode host, game, or network call here.
            (root / "sitecustomize.py").write_text("import time; time.sleep(10)\n")
            adapter = root / "adapter.py"
            adapter.write_text("print('HARNESS_OK {\"decision\":\"allow\"}')\n")
            module = Path(__file__).resolve().parents[1] / "modding_harness/opencode_plugin.mjs"
            driver = root / "driver.mjs"
            driver.write_text("import { createGuard } from " + json.dumps(module.as_uri()) + ";\n" +
                              "const hooks = await createGuard({adapter:" + json.dumps(str(adapter)) +
                              "})({directory:" + json.dumps(str(root)) + "});\n" +
                              "try { await hooks['tool.execute.before']({tool:'write',callID:'c'}, {args:{filePath:'a'}});"
                              " console.log('unexpected allow'); } catch(e) { console.log(e.message); }\n")
            start = time.monotonic()
            result = subprocess.run([node, str(driver)], capture_output=True, text=True, timeout=20,
                                    env=dict(os.environ, MODDING_HARNESS_PYTHON=sys.executable,
                                             MODDING_HARNESS_NO_PYTHON_FALLBACK="1",
                                             MODDING_HARNESS_OPENCODE_TIMEOUT_S="2", PYTHONPATH=str(root)))
            elapsed = time.monotonic() - start
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("GUARD INERT", result.stdout)
            self.assertLess(elapsed, 4, "Python discovery escaped the two-second transport budget")


if __name__ == "__main__":
    unittest.main()
