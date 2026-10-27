import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from modding_harness.preservation import PreservationFailed, SaveStore


class ImportTests(unittest.TestCase):
    def test_import_records_history_without_reading_or_changing_live_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "live.ess"
            live.write_bytes(b"current")
            old = root / "backup.ess"
            old.write_bytes(b"old")
            store = SaveStore(root / "workspace", reserve=0)
            record = {"original": str(live), "source": str(old), "missing": False,
                      "sha256": hashlib.sha256(b"old").hexdigest(), "size": 3}
            missing = {"original": str(live.with_suffix(".skse")), "source": str(root / "absent.skse"), "missing": True}
            transaction = store.import_records([[record, missing]])
            self.assertEqual(transaction["purpose"], "recovery-import")
            self.assertEqual(live.read_bytes(), b"current")
            output = store.restore(transaction["id"])
            self.assertEqual(Path(output["published"][0]).read_bytes(), b"old")
            record["sha256"] = "0" * 64
            with self.assertRaises(PreservationFailed):
                store.import_records([[record, missing]])
            self.assertEqual(len(list((store.root / "transactions").glob("*.json"))), 1)

    def test_malformed_restore_authority_is_a_refusal_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SaveStore(tmp, reserve=0)
            directory = store.root / "transactions"
            directory.mkdir(parents=True)
            identity = "a" * 32
            for value in ([], {"v": 1, "id": identity, "complete": True, "groups": [None]},
                          {"v": 1, "id": identity, "complete": True,
                           "groups": [{"logical_save": 0, "members": [None]}]}):
                (directory / (identity + ".json")).write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    store.load(identity)


if __name__ == "__main__":
    unittest.main()
