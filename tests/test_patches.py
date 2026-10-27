import json
import unittest
from pathlib import Path

from modding_harness import patches


class PatchTests(unittest.TestCase):
    def test_every_captured_codex_shape_has_its_measured_files_or_refuses(self):
        oracle = json.loads((Path(__file__).parent / "fixtures/codex-0.160.0-patch-oracle.json").read_text(encoding="utf-8"))
        self.assertEqual(oracle["codex_version"], "0.160.0")
        rows = oracle["rows"]
        self.assertGreaterEqual(len(rows), 60)
        self.assertEqual(len({row["name"] for row in rows}), len(rows))
        effect = {"add": "added", "update": "changed", "delete": "removed", "move_from": "removed", "move_to": "added"}
        for row in rows:
            with self.subTest(row=row["name"]):
                if row["rc"] == 0:
                    named = {}
                    for op, path in patches.parse_patch(row["patch"]):
                        named.setdefault(path, set()).add(effect[op])
                    self.assertEqual(set(named), set(row["touched"]))
                    self.assertTrue(all(row["touched"][p] in named[p] for p in named))
                else:
                    with self.assertRaises(patches.PatchParseError):
                        patches.parse_patch(row["patch"])

    def test_host_specific_header_and_whitespace_rules_are_not_unified(self):
        text = "*** Begin Patch\n*** Add File: a\n+x\n  *** Delete File: b\n*** End Patch"
        self.assertEqual(patches.parse_patch(text), [("add", "a"), ("delete", "b")])
        self.assertEqual(patches.parse_patch_opencode(text), [("add", "a")])
        text = "*** Begin Patch\n*** Add File:a\n+x\n*** End Patch"
        self.assertEqual(patches.parse_patch_opencode(text), [("add", "a")])
        with self.assertRaises(patches.PatchParseError):
            patches.parse_patch(text)


if __name__ == "__main__":
    unittest.main()
