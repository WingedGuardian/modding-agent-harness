import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from modding_harness import profiles
from modding_harness.contracts import Action, Result
from modding_harness.generic import file_policy, handle, parse
from modding_harness.preservation import SaveStore


class GenericTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "saves"
        self.folder.mkdir()
        self.save = self.folder / "progress.save"
        self.save.write_bytes(b"third-game bytes")
        config = self.root / ".modding-agent-harness/config.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"save_reserve_bytes": 0}))
        self.profile = profiles.toy([self.folder])
        self.action = asdict(Action("unspecified-agent", "edit", "files", cwd=str(self.root),
                            paths=(str(self.save),), operations=("write",)))

    def call(self, action=None, **options):
        return handle(json.dumps(self.action if action is None else action), self.root, self.profile,
                      file_policy(self.profile), **options)

    def test_third_game_preserves_then_restores_without_a_named_agent_directory(self):
        result = self.call()
        self.assertEqual(result["result"]["preservation"], "verified")
        self.save.write_bytes(b"new state")
        store = SaveStore(self.root, reserve=0)
        restored = store.restore(result["transactions"][0], self.root / "recovered")
        self.assertEqual(Path(restored["published"][0]).read_bytes(), b"third-game bytes")
        self.assertFalse((self.root / ".claude").exists())
        self.assertFalse((self.root / ".codex").exists())

    def test_unknown_surface_is_explicit_and_local_restriction_still_wins(self):
        self.action.update(surface="nested-code-mode", paths=[], operations=[])
        result = self.call()
        self.assertEqual((result["result"]["decision"], result["result"]["evaluation"]), ("allow", "unguarded"))
        self.assertEqual(result["unknown_occurrences"], 1)
        result = self.call(local_restrictions=(lambda action: Result("deny", "evaluated", reason="installed local restriction"),))
        self.assertEqual(result["result"]["decision"], "deny")

    def test_whole_folder_and_unresolved_wildcards_refuse(self):
        for path in (str(self.folder), str(self.folder / "*.save")):
            self.action["paths"] = [path]
            self.assertEqual(self.call()["result"]["decision"], "deny")

    def test_preview_is_not_a_pre_tool_execution_receipt(self):
        from modding_harness.telemetry import Telemetry
        result = self.call(preview=True)
        self.assertEqual(result["result"]["preservation"], "required")
        self.assertFalse(result["transactions"])
        self.assertEqual(Telemetry(self.root).report("")["allowed_pre_without_post"], 0)

    def test_malformed_known_requests_never_become_unknown_allows(self):
        for update in ({"api_version": True}, {"operations": []}, {"command": 12}, {"unrecognized": "x"}):
            result = self.call(dict(self.action, **update))
            self.assertEqual((result["result"]["decision"], result["result"]["evaluation"]), ("deny", "failed"))
        with self.assertRaises(ValueError):
            parse('{"api_version":1,"api_version":1}')


if __name__ == "__main__":
    unittest.main()
