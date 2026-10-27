"""Native missing-tail resolution stays bounded without changing physical identity."""
import os
import ctypes
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modding_harness import preservation, profiles


def winerror(code):
    error = OSError("owned native error fixture")
    error.winerror = code
    return error


@unittest.skipUnless(os.name == "nt", "Windows native missing-tail resolver")
class MissingTailTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_missing_tail_needs_bounded_native_probes_and_one_boundary_resolution(self):
        target = self.root / "MISSING" / Path(*(["a"] * 10000)) / "last"
        for code in (2, 3):
            calls = []
            def resolve(value, strict=False):
                calls.append((value, strict))
                if strict:
                    if "MISSING" in value.parts:
                        raise winerror(code)
                    return value
                self.assertEqual(value, self.root / "MISSING")
                return value
            with self.subTest(winerror=code), patch.object(Path, "resolve", resolve):
                result = profiles.canonical(target)
            self.assertEqual(os.path.normcase(str(target)), result)
            self.assertLessEqual(len(calls), 20)
            self.assertEqual(1, sum(not strict for _, strict in calls))

    def test_non_missing_native_errors_keep_the_original_resolution_contract(self):
        target = self.root / Path(*(["a"] * 40)) / "missing"
        fallback = self.root / "native-fallback"
        for code in (5, 32, 123):
            with self.subTest(winerror=code):
                def resolve(value, strict=False):
                    if strict:
                        raise winerror(code)
                    self.assertEqual(value, target)
                    return fallback
                with patch.object(Path, "resolve", resolve):
                    self.assertEqual(os.path.normcase(str(fallback)), profiles.canonical(target))

    def test_a_non_missing_error_during_prefix_search_also_falls_back(self):
        target = self.root / Path(*(["a"] * 40)) / "missing"
        fallback = self.root / "resolved-by-original"
        def resolve(value, strict=False):
            if not strict:
                self.assertEqual(value, target)
                return fallback
            if value == target:
                raise winerror(2)
            if len(value.parts) > 1:
                raise winerror(5)
            return value
        with patch.object(Path, "resolve", resolve):
            self.assertEqual(os.path.normcase(str(fallback)), profiles.canonical(target))

    def test_missing_drive_anchor_resolves_only_the_short_native_boundary(self):
        target = Path("Q:/") / Path(*(["a"] * 10000)) / "last"
        for code in (2, 3):
            calls = []
            def resolve(value, strict=False):
                calls.append((value, strict))
                if strict:
                    raise winerror(code)
                self.assertEqual(value, Path("Q:/"))
                return value
            with self.subTest(winerror=code), patch.object(Path, "resolve", resolve):
                self.assertEqual(os.path.normcase(str(target)), profiles.canonical(target))
            self.assertEqual(3, len(calls))

    def test_a_non_missing_drive_anchor_error_delegates_the_original_operand(self):
        target = Path("Q:/") / Path(*(["a"] * 40)) / "last"
        fallback = Path("Q:/native-original")
        for code in (5, 21, 53):
            def resolve(value, strict=False):
                if strict:
                    raise winerror(3 if value == target else code)
                self.assertEqual(value, target)
                return fallback
            with self.subTest(winerror=code), patch.object(Path, "resolve", resolve):
                self.assertEqual(os.path.normcase(str(fallback)), profiles.canonical(target))

    def test_actual_absent_drive_and_namespace_spellings_match_native_resolution(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetLogicalDrives.argtypes = []
        kernel.GetLogicalDrives.restype = ctypes.c_uint32
        present = kernel.GetLogicalDrives()
        self.assertNotEqual(0, present, "GetLogicalDrives could not qualify the fixture")
        drive = next((chr(ord("Z") - i) for i in range(24)
                      if not present & (1 << (25 - i))), None)
        if drive is None:
            self.skipTest("no absent drive letter is available for native identity observation")
        anchor = Path(drive + ":/")
        with self.assertRaises(OSError) as missing:
            anchor.resolve(strict=True)
        self.assertIn(missing.exception.winerror, (2, 3))
        tail = "\\".join(["a"] * 64) + "\\last"
        for prefix in ("", "\\\\?\\", "\\\\.\\"):
            value = prefix + drive + ":\\" + tail
            with self.subTest(prefix=prefix):
                expected = profiles.comparison_key(str(Path(value).resolve()))
                self.assertEqual(expected, profiles.canonical(value))
                store = drive + ":\\a"
                parent = profiles.comparison_key(str(Path(store).resolve()))
                try:
                    native_inside = os.path.commonpath([parent, expected]) == parent
                except ValueError:
                    native_inside = False
                self.assertEqual(native_inside, preservation.contains(store, value))
                self.assertEqual(native_inside, preservation.protects(value, [store]))

    def test_missing_unc_anchor_keeps_native_referral_semantics(self):
        target = Path("//owned.invalid/share") / Path(*(["a"] * 40)) / "last"
        fallback = Path("//owned.invalid/share/native-original")
        def resolve(value, strict=False):
            if strict:
                raise winerror(3)
            self.assertEqual(value, target)
            return fallback
        with patch.object(Path, "resolve", resolve):
            self.assertEqual(os.path.normcase(str(fallback)), profiles.canonical(target))

    def test_missing_drive_native_boundary_errors_are_not_waived(self):
        target = Path("Q:/") / Path(*(["a"] * 40)) / "last"
        def resolve(value, strict=False):
            if strict:
                raise winerror(3)
            self.assertEqual(value, Path("Q:/"))
            raise winerror(5)
        with patch.object(Path, "resolve", resolve), self.assertRaises(OSError) as error:
            profiles.canonical(target)
        self.assertEqual(5, error.exception.winerror)

    def test_parent_components_and_raw_dot_components_keep_original_semantics(self):
        tail = "\\".join(["a"] * 40)
        values = (str(self.root) + "\\missing\\..\\" + tail,
                  "\\\\?\\" + str(self.root) + "\\.\\" + tail,
                  "\\\\.\\" + str(self.root) + "\\.\\" + tail)
        fallback = self.root / "original-result"
        for value in values:
            with self.subTest(value=value):
                def resolve(path, strict=False):
                    self.assertFalse(strict, "dot semantics must use the original non-strict resolver")
                    return fallback
                with patch.object(Path, "resolve", resolve):
                    self.assertEqual(os.path.normcase(str(fallback)), profiles.canonical(value))

    def junction(self, link, target):
        proc = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.addCleanup(lambda: os.path.lexists(link) and link.rmdir())

    def test_actual_junctions_dangling_targets_and_namespace_aliases_match_native_resolution(self):
        target = self.root / "store"
        target.mkdir()
        link = self.root / "alias"
        self.junction(link, target)
        absent = self.root / "absent-store"
        dangling = self.root / "dangling"
        self.junction(dangling, absent)
        suffix = Path(*(["a"] * 64)) / "blob"
        for base, store in ((link, target), (dangling, absent), (target, target)):
            path = base / suffix
            for prefix in ("", "\\\\?\\", "\\\\.\\"):
                value = prefix + str(path)
                with self.subTest(base=base.name, prefix=prefix):
                    expected = profiles.comparison_key(str(Path(value).resolve()))
                    self.assertEqual(expected, profiles.canonical(value))
                    self.assertTrue(preservation.protects(value, [store]))
                    self.assertFalse(preservation.protects(value, [self.root / "sibling-store"]))
        self.assertTrue(preservation.protects(self.root, [link], ancestor=True))

    def test_long_rooted_and_relative_spellings_keep_native_cwd_resolution(self):
        target = self.root / "missing" / Path(*(["a"] * 64)) / "blob"
        rooted = str(target)[len(target.drive):]
        relative = os.path.relpath(target)
        for value in (rooted, relative):
            with self.subTest(value=value):
                self.assertEqual(profiles.comparison_key(str(Path(value).resolve())), profiles.canonical(value))

    def test_per_drive_relative_operands_are_delegated_without_guessing_a_cwd(self):
        for drive in ("C:", "D:"):
            value = drive + "\\".join(["a"] * 40) + "\\missing"
            expected = Path(drive + "\\native-per-drive-cwd\\missing")
            def resolve(path, strict=False):
                self.assertFalse(strict)
                self.assertEqual(str(path), value)
                return expected
            with self.subTest(drive=drive), patch.object(Path, "resolve", resolve):
                self.assertEqual(os.path.normcase(str(expected)), profiles.canonical(value))

    def test_colon_components_before_and_after_missing_boundary_match_native_resolution(self):
        # No streams/files are opened: these are native identity observations of
        # owned absent paths. Path(*parts) and joinpath must not reinterpret an
        # embedded drive-shaped component on either side of the missing boundary.
        other = "Q:" if self.root.drive.casefold() != "q:" else "R:"
        components = (self.root.drive + "stream.xml.gz", other + "stream.xml.gz",
                      "member:stream.xml.gz")
        tail = "\\".join(["a"] * 40)
        for component in components:
            for suffix in ("MISSING\\" + tail + "\\" + component,
                           component + "\\MISSING\\" + tail):
                value = str(self.root) + "\\" + suffix
                with self.subTest(component=component, before=suffix.startswith(component)):
                    expected = profiles.comparison_key(str(Path(value).resolve()))
                    self.assertEqual(expected, profiles.canonical(value))

    def test_raw_directory_suffix_remains_distinct_and_original_io_spelling_is_kept(self):
        for suffix in (".", " "):
            raw = Path("\\\\?\\" + str(self.root / "Saves") + suffix)
            raw.mkdir()
            self.addCleanup(raw.rmdir)
            target = raw / Path(*(["a"] * 40)) / "Owned.xml.gz"
            parents = []
            current = raw
            for _ in range(40):
                current = current / "a"
                current.mkdir()
                parents.append(current)
            for parent in parents:
                self.addCleanup(parent.rmdir)
            target.write_bytes(b"owned raw namespace bytes")
            self.addCleanup(target.unlink)
            expected = profiles.comparison_key(str(target.resolve()))
            self.assertEqual(expected, profiles.canonical(target))
            folded = str(target)[4:].replace("Saves" + suffix + "\\", "Saves\\")
            self.assertNotEqual(profiles.canonical(target), profiles.canonical(folded))
            store = preservation.SaveStore(self.root / ("history-dot" if suffix == "." else "history-space"), reserve=0)
            record = store.backup([profiles.x4().members(target)])
            member = record["groups"][0]["members"][0]
            self.assertEqual(str(target), member["path"])
            self.assertTrue(os.path.samefile(member["path"], target))
            self.assertEqual(target.read_bytes(), (store.root / "blobs" / member["sha256"]).read_bytes())


class ComparisonReuseTests(unittest.TestCase):
    def test_protection_resolves_each_operand_once_without_a_cross_call_cache(self):
        calls = []
        inside = os.path.abspath("owned-store/child")
        store = os.path.abspath("owned-store")
        outside = os.path.abspath("owned-other")
        current = {"operand": inside, "first": store, "second": outside}
        def canonical(value):
            calls.append(value)
            return current[value]
        with patch.object(preservation, "canonical", canonical):
            self.assertTrue(preservation.protects("operand", ["first"], ancestor=True))
            self.assertEqual(["first", "operand"], calls)
            calls.clear()
            current["operand"] = outside
            self.assertFalse(preservation.protects("operand", ["first"], ancestor=True))
            self.assertEqual(["first", "operand"], calls)

    def test_empty_stores_do_not_resolve_and_os_errors_are_not_waived(self):
        with patch.object(preservation, "canonical", side_effect=OSError("unreadable identity")):
            self.assertFalse(preservation.protects("unused", []))
            with self.assertRaises(OSError):
                preservation.protects("operand", ["store"])
            with self.assertRaises(OSError):
                preservation.contains("store", "operand")

    def test_disjoint_drives_and_invalid_comparisons_remain_false(self):
        with patch.object(preservation, "canonical", side_effect=ValueError("not comparable")):
            self.assertFalse(preservation.protects("operand", ["store"], ancestor=True))
            self.assertFalse(preservation.contains("store", "operand"))
        if os.name == "nt":
            with patch.object(preservation, "canonical", side_effect=lambda value: value):
                self.assertFalse(preservation.protects("D:\\operand", ["C:\\store"], ancestor=True))


if __name__ == "__main__":
    unittest.main()
