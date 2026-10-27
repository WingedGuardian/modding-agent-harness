"""Native path identity without changing the spelling used to read saved bytes."""
import os
from pathlib import Path, PureWindowsPath
import tempfile
import unittest
from unittest.mock import patch

from modding_harness.preservation import SaveStore, contains, protects
from modding_harness.profiles import canonical, skyrim, x4


@unittest.skipUnless(os.name == "nt", "Windows native extended path identities")
class WindowsPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.saves = self.root / "Saves"
        self.saves.mkdir()

    @staticmethod
    def extended(path):
        return Path("\\\\?\\" + str(path))

    def test_existing_and_missing_local_drive_members_compare_in_both_directions(self):
        existing = self.saves / "MiXeD.xml.gz"
        existing.write_bytes(b"owned opaque save")
        for target in (existing, self.saves / "Missing.xml.gz"):
            with self.subTest(target=target.name):
                alias = self.extended(target)
                self.assertEqual(canonical(target), canonical(alias))
                self.assertTrue(contains(self.saves, alias))
                self.assertTrue(contains(self.extended(self.saves), target))
        self.assertTrue(os.path.samefile(existing, self.extended(existing)))

    def test_store_and_ancestor_protection_recognize_extended_aliases(self):
        store = self.root / "store"
        store.mkdir()
        self.assertTrue(protects(self.extended(store / "blob"), [store]))
        self.assertTrue(protects(self.root, [self.extended(store)], ancestor=True))
        self.assertFalse(protects(self.root / "unrelated", [self.extended(store)]))
        with self.assertRaisesRegex(ValueError, "separate"):
            SaveStore(self.root, self.extended(self.saves / "store"), [self.saves], reserve=0)

    def test_preservation_keeps_original_io_spelling_and_rejects_duplicate_aliases(self):
        target = self.saves / "MiXeD.xml.gz"
        target.write_bytes(b"owned opaque save")
        alias = self.extended(target)
        store = SaveStore(self.root, save_roots=[self.saves], reserve=0)
        record = store.backup([x4().members(alias)])
        member = record["groups"][0]["members"][0]
        self.assertEqual(str(alias), member["path"])
        self.assertEqual(target.read_bytes(), (store.root / "blobs" / member["sha256"]).read_bytes())
        self.assertEqual(b"owned opaque save", target.read_bytes())
        with self.assertRaisesRegex(OSError, "ambiguous"):
            store.backup([[target, alias]])

    def test_pair_members_still_use_the_original_extended_directory(self):
        target = self.saves / "MiXeD.ess"
        target.write_bytes(b"ESS fixture")
        target.with_suffix(".skse").write_bytes(b"SKSE fixture")
        members = skyrim().members(self.extended(target))
        self.assertTrue(all(str(member).startswith("\\\\?\\") for member in members))
        self.assertEqual([b"ESS fixture", b"SKSE fixture"], [member.read_bytes() for member in members])
        record = SaveStore(self.root, save_roots=[self.saves], reserve=0).backup([members])
        self.assertEqual(2, len(record["groups"][0]["members"]))

    def test_resolved_unc_comparison_only_without_network_io(self):
        # Stub only the completed resolve result; never access a network share.
        for resolved, expected in (
            ("\\\\?\\UNC\\Server\\Share\\MiXeD.xml.gz", "\\\\Server\\Share\\MiXeD.xml.gz"),
            ("\\\\?\\C:\\MiXeD\\..\\File.xml.gz", "C:\\MiXeD\\..\\File.xml.gz"),
            ("\\\\?\\Volume{fixture}\\MiXeD.xml.gz", "\\\\?\\Volume{fixture}\\MiXeD.xml.gz"),
        ):
            with self.subTest(resolved=resolved), patch(
                    "modding_harness.profiles.Path.resolve", return_value=PureWindowsPath(resolved)):
                self.assertEqual(os.path.normcase(expected), canonical("unused"))

    def legacy_record(self, duplicate=False):
        target = self.saves / "Legacy.xml.gz"
        target.write_bytes(b"legacy owned save")
        alias = self.extended(target)
        store = SaveStore(self.root, save_roots=[self.saves], reserve=0)
        # The exact canonical algorithm used before extended-key normalization.
        old_key = lambda path: os.path.normcase(str(Path(path).resolve()))
        with patch("modding_harness.preservation.canonical", old_key), patch(
                "modding_harness.preservation.comparison_key", lambda key: key):
            record = store.backup([[target, alias] if duplicate else [alias]])
        return store, record, target, alias

    def test_existing_legacy_extended_transaction_selected_restore_retains_manifest(self):
        store, record, target, alias = self.legacy_record()
        manifest = store.root / "transactions" / (record["id"] + ".json")
        original_manifest = manifest.read_bytes()
        for index, selected in enumerate(((), (target,), (alias,))):
            result = store.restore(record["id"], self.root / ("restored-" + str(index)), selected=selected)
            self.assertEqual(1, len(result["published"]))
            self.assertEqual(b"legacy owned save", Path(result["published"][0]).read_bytes())
        self.assertEqual(original_manifest, manifest.read_bytes())
        self.assertEqual(b"legacy owned save", target.read_bytes())

    def test_existing_legacy_extended_transaction_revalidates_unchanged_source(self):
        store, record, _target, _alias = self.legacy_record()
        store.revalidate(record["groups"])

    def test_legacy_transaction_with_duplicate_extended_aliases_is_ambiguous(self):
        store, record, _target, _alias = self.legacy_record(duplicate=True)
        with self.assertRaisesRegex(ValueError, "member path"):
            store.inspect(record["id"])


@unittest.skipIf(os.name == "nt", "POSIX literal backslashes")
class PosixPathTests(unittest.TestCase):
    def test_literal_namespace_like_name_is_not_reinterpreted_on_posix(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "\\\\?\\C:\\MiXeD.xml.gz"
            target.write_bytes(b"literal POSIX filename")
            self.assertEqual(str(target.resolve()), canonical(target))
