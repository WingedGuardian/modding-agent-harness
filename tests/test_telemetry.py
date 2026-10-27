import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from modding_harness.contracts import Action, Result, unknown_tool
from modding_harness.telemetry import Telemetry, announce


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = Telemetry(self.root)
        self.action = Action("future-agent", "mcp__new__tool", "mcp", session_id="private-session",
                             tool_call_id="private-call", command="SECRET argument and private/path")

    def test_unknown_records_every_occurrence_with_sparse_announcement_and_no_secrets(self):
        counts = [self.log.record(self.action, "pre", unknown_tool(self.action)) for _ in range(9)]
        self.assertEqual(counts, list(range(1, 10)))
        self.assertEqual([n for n in counts if announce(n)], [1, 2, 4, 8])
        self.assertEqual(self.log.report("private-session")["unknown_occurrences"],
                         {"future-agent:mcp__new__tool": 9})
        raw = self.log.path.read_bytes()
        for secret in (b"SECRET", b"private-session", b"private-call", b"private/path"):
            self.assertNotIn(secret, raw)

    def test_delayed_post_and_missing_ids_remain_distinct(self):
        allowed = Result(evaluation="evaluated")
        self.log.record(self.action, "pre", allowed)
        denied = replace(self.action, tool_call_id="denied")
        self.log.record(denied, "pre", Result("deny", "evaluated"))
        self.log.record(replace(self.action, tool_call_id=""), "post", allowed)
        report = self.log.report("private-session")
        self.assertEqual((report["allowed_pre_without_post"], report["uncorrelated_receipts"]), (1, 1))
        self.log.record(self.action, "post", replace(allowed, validation="passed"))
        report = self.log.report("private-session")
        self.assertEqual((report["allowed_pre_without_post"], report["correlated_calls"]), (0, 1))
        self.assertEqual(self.log.report("other-session")["evidence"], "unavailable")

    def test_unavailable_logging_does_not_change_policy(self):
        self.log.path.parent.mkdir()
        self.log.path.write_bytes(b"not a database")
        self.assertIsNone(self.log.record(self.action, "pre", unknown_tool(self.action)))
        self.assertEqual(self.log.report("private-session")["evidence"], "unavailable")


if __name__ == "__main__":
    unittest.main()
