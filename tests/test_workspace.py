import json
import tempfile
import unittest
from pathlib import Path

from modding_harness import profiles, workspace


class WorkspaceTests(unittest.TestCase):
    def test_neutral_default_and_custom_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(workspace.save_store(root, profiles.toy()).root,
                             (root / ".modding-agent-harness/backups/saves").resolve())
            config = root / ".modding-agent-harness/config.json"
            config.parent.mkdir()
            config.write_text(json.dumps({"v": 1, "save_store": "other", "save_reserve_bytes": 0}))
            self.assertEqual(workspace.save_store(root, profiles.toy()).root, (root / "other").resolve())
            config.write_text(json.dumps({"save_store": "saves/backups"}))
            with self.assertRaises(ValueError):
                workspace.save_store(root, profiles.toy([root / "saves"]))

    def test_bad_config_never_silently_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".modding-agent-harness/config.json"
            config.parent.mkdir()
            for data in ([], {"v": 2}, {"save_reserve_bytes": True}, {"save_store": ""}, {"typo": 1}):
                config.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    workspace.settings(tmp)


if __name__ == "__main__":
    unittest.main()
