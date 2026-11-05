"""modding_harness.save_lock logic. No real ACL is changed here (a locked temp file could not be cleaned up without an
elevated prompt); the OS behaviour was proven on Windows 11 against 18 delete/overwrite routes (2026-10-30), and
the elevated unlock path live (2026-10-31). Windows calls are replaced with recorders."""
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from modding_harness import save_lock as sl

EXTS = (".ess", ".skse")
LOCKED = "D:AI(D;;DCLCCRSDWDWO;;;S-1-5-21-1)(A;;FR;;;OW)(A;ID;FA;;;SY)"
FOLDER_LOCKED = "D:AI(D;;DTSDWDWO;;;S-1-5-21-1)(A;;FR;;;OW)(A;OICIID;FA;;;SY)"
PLAIN = "D:AI(A;ID;FA;;;SY)"


class SaveLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.calls = []
        for name, value in (("supported", lambda: True), ("user_sid", lambda: "S-1-5-21-1"),
                            ("is_elevated", lambda: False),
                            ("lock_file", lambda p, sid: self.calls.append(("file", Path(p).name))),
                            ("lock_folder", lambda p, sid: self.calls.append(("folder", str(p)))),
                            ("is_locked", lambda p: False), ("has_acls", lambda p: True)):
            p = patch.object(sl, name, value)
            p.start()
            self.addCleanup(p.stop)

    def pair(self, stem, age=3600, folder=None):
        folder = folder or self.d
        for ext in EXTS:
            p = folder / (stem + ext)
            p.write_bytes(b"e")
            t = time.time() - age
            os.utime(p, (t, t))
        return folder / (stem + ".ess")

    def test_rotating_slots_and_fresh_saves_are_left_unlocked(self):
        self.pair("Save1_x"); self.pair("Autosave2_x"); self.pair("Quicksave0_x"); self.pair("Save9_x", age=10)
        todo, skipped = sl.candidates(self.d, EXTS)
        self.assertEqual({"Save1_x.ess", "Save1_x.skse"}, {Path(p).name for p in todo})
        self.assertEqual(4, len([s for s in skipped if s[1] == "rotating slot"]))
        todo, _ = sl.candidates(self.d, EXTS, rotating=None)
        self.assertIn("Autosave2_x.ess", {Path(p).name for p in todo})

    def acls(self, table):
        """Serve DACLs by file name (default plain) through the one read every check goes through."""
        return patch.object(sl, "_read_sddl", lambda p: table.get(os.path.basename(str(p)), PLAIN))

    def test_status_map_reads_each_saves_rules_and_paths_may_hold_spaces(self):
        folder = self.d / "My Games"
        folder.mkdir()
        self.pair("A", folder=folder); self.pair("B", folder=folder)
        with self.acls({"A.ess": LOCKED}):
            m = sl.status_map(folder, EXTS)
        self.assertTrue(m[str(folder / "A.ess")])
        self.assertFalse(m[str(folder / "B.ess")])

    def test_a_rule_is_ours_by_its_shape(self):
        other = "S-1-5-21-9-500"
        for sddl, locked, plain in (
                (LOCKED, True, False),
                (FOLDER_LOCKED, True, False),
                ("D:(D;;DCLCCRSDWDWO;;;%s)" % other, True, False),      # another account, no owner rule
                ("D:(D;;DCLCCRSDWDWO;;;LA)", True, False),              # an account SDDL names by alias
                ("D:(D;;0x000d0106;;;%s)" % other, True, False),        # the mask spelled in hex
                ("D:(A;;FR;;;OW)", False, False),                       # owner rule alone: not a lock, not plain
                ("D:(D;ID;DCLCCRSDWDWO;;;%s)" % other, False, True),    # inherited: not ours
                ("D:(D;OICI;DCLCCRSDWDWO;;;%s)" % other, False, True),  # inheriting: not ours
                ("D:(D;;DCLC;;;%s)" % other, False, True),              # another shape: someone else's
                ("D:(A;;DCLCCRSDWDWO;;;%s)" % other, False, True),      # an allow, not a deny
                ("D:(A;ID;FA;;;OW)", False, True),                      # an inherited owner rule (temp folders have it)
                ("D:(D;;FR;;;OW)", False, True),                        # a deny naming OWNER RIGHTS is not the grant
                (PLAIN, False, True),
                ("D:NO_ACCESS_CONTROL", False, True)):
            shaped = any(sl._is_lock_deny(a) for a in sl._aces(sddl))
            self.assertEqual((locked, plain), (shaped, sl._sddl_plain(sddl)), sddl)

    def test_locked_means_the_rule_binds_this_user(self):
        me, other = "S-1-5-21-1", "S-1-5-21-9-500"
        for sddl, binds in ((LOCKED, True),
                            (FOLDER_LOCKED, True),
                            ("D:(D;;0x000d0106;;;%s)" % me, True),              # the mask spelled in hex
                            ("D:(D;;DCLCCRSDWDWO;;;%s)" % other, False),        # another account's rule binds them
                            ("D:(D;ID;DCLCCRSDWDWO;;;%s)" % me, False),         # inherited: not ours
                            ("D:(D;OICI;DCLCCRSDWDWO;;;%s)" % me, False),       # inheriting: not ours
                            ("D:(D;;DCLC;;;%s)" % me, False),                   # another shape: not this module's lock
                            ("D:(A;;DCLCCRSDWDWO;;;%s)" % me, False),           # an allow, not a deny
                            ("D:(A;;FR;;;OW)", False),
                            (PLAIN, False),
                            ("D:NO_ACCESS_CONTROL", False)):
            self.assertEqual(binds, sl._sddl_binds(sddl, me), sddl)

    def test_a_rule_naming_this_user_by_alias_binds_it(self):
        with patch.object(sl, "_alias_sid", lambda alias: "S-1-5-21-1" if alias == "LA" else "S-1-5-32-546"):
            self.assertTrue(sl._sddl_binds("D:(D;;DCLCCRSDWDWO;;;LA)", "S-1-5-21-1"))
            self.assertFalse(sl._sddl_binds("D:(D;;DCLCCRSDWDWO;;;BG)", "S-1-5-21-1"))

    def test_TWIN_an_sid_is_compared_as_written_and_never_looked_up_as_an_alias(self):
        def refuse(alias):
            raise AssertionError("looked up " + alias)
        with patch.object(sl, "_alias_sid", refuse):
            self.assertTrue(sl._sddl_binds(LOCKED, "S-1-5-21-1"))
            self.assertFalse(sl._sddl_binds(LOCKED, "S-1-5-21-2"))

    def test_status_map_reports_another_accounts_rule_as_not_protecting_this_user(self):
        folder = self.d / "f"
        folder.mkdir()
        self.pair("A", folder=folder)
        foreign = "D:(D;;DCLCCRSDWDWO;;;S-1-5-21-9-500)(A;;FR;;;OW)"
        with self.acls({"A.ess": foreign, "A.skse": LOCKED}):
            self.assertEqual({str(folder / "A.ess"): False, str(folder / "A.skse"): True}, sl.status_map(folder, EXTS))

    def test_text_that_is_not_a_dacl_raises_and_never_reads_as_plain(self):
        for text in ("", "O:BA", "garbage"):
            with self.assertRaises(OSError):
                sl._sddl_plain(text)
            with self.assertRaises(OSError):
                sl._sddl_binds(text, "S-1-5-21-1")

    def test_an_unlock_that_did_not_take_or_cannot_be_confirmed_raises(self):
        with patch.object(sl, "_enable_restore_privilege", lambda: None), \
                patch.object(sl, "_rewrite_dacl", lambda p, edit: True):
            with self.acls({"A.ess": LOCKED}):
                with self.assertRaises(PermissionError) as raised:
                    sl.unlock(self.d / "A.ess", "S-1")
                self.assertIn("did not take", str(raised.exception))

            def refuse(p):
                raise OSError("denied")
            with patch.object(sl, "_read_sddl", refuse):
                with self.assertRaises(PermissionError) as raised:
                    sl.unlock(self.d / "A.ess", "S-1")
                self.assertIn("could not be confirmed", str(raised.exception))
            with self.acls({}):
                sl.unlock(self.d / "A.ess", "S-1")                              # the twin: it took

    def test_a_save_that_cannot_be_read_refuses_the_whole_listing(self):
        self.pair("A")

        def refuse(p):
            raise PermissionError("denied")
        with patch.object(sl, "_read_sddl", refuse):
            with self.assertRaises(OSError) as raised:
                sl.status_map(self.d, EXTS)
        self.assertIn("A.", str(raised.exception))

    def test_TWIN_a_save_gone_since_the_listing_is_left_out(self):
        self.pair("A")

        def gone(p):
            if str(p).endswith(".ess"):
                raise FileNotFoundError("gone")
            return PLAIN
        with patch.object(sl, "_read_sddl", gone):
            self.assertEqual([str(self.d / "A.skse")], list(sl.status_map(self.d, EXTS)))

    def test_unlock_strips_only_our_explicit_rules(self):
        me, other = "S-1-5-21-1-1003", "S-1-5-21-9-500"
        sddl = ("D:AI(D;;DTSDWDWO;;;%s)(A;;FR;;;OW)(D;;WD;;;%s)(D;ID;WD;;;%s)(A;ID;FR;;;OW)(A;OICIID;FA;;;%s)"
                % (me, other, me, me))
        self.assertEqual("D:AI(D;;WD;;;%s)(D;ID;WD;;;%s)(A;ID;FR;;;OW)(A;OICIID;FA;;;%s)" % (other, me, me),
                         sl.strip_lock_aces(sddl, me))

    def test_unlock_from_another_account_strips_the_lock_rule_that_names_the_original_user(self):
        admin = "S-1-5-21-9-500"
        self.assertEqual(PLAIN, sl.strip_lock_aces(LOCKED, admin))
        self.assertEqual("D:(A;OICIID;FA;;;SY)", sl.strip_lock_aces("D:(D;;DTSDWDWO;;;LA)(A;OICIID;FA;;;SY)", admin))

    def test_TWIN_another_accounts_deny_of_another_shape_is_never_stripped(self):
        kept = "D:(D;;WD;;;S-1-5-21-8-1000)(D;;FA;;;BG)"
        self.assertEqual(kept, sl.strip_lock_aces(kept, "S-1-5-21-9-500"))

    def test_any_explicit_deny_naming_the_unlocking_account_is_stripped(self):
        """A merged mask (icacls joins a new deny to an existing one for the same account) is no longer lock-shaped."""
        self.assertEqual(PLAIN, sl.strip_lock_aces("D:AI(D;;FW;;;S-1-5-21-1)(A;ID;FA;;;SY)", "S-1-5-21-1"))

    def test_a_failed_backup_unlocks_nothing_and_a_good_one_unlocks_the_group(self):
        a = self.pair("A")
        unlocked = []
        with patch.object(sl, "unlock", lambda p, sid: unlocked.append(Path(p).name)):
            def broken(paths):
                raise OSError("disk full")
            with self.assertRaises(OSError):
                sl.backup_then_unlock(a, [a.with_suffix(".skse")], "S-1", broken)
            self.assertEqual([], unlocked)
            backed = []
            sl.backup_then_unlock(a, [a.with_suffix(".skse"), a.with_suffix(".missing")], "S-1", backed.extend)
        self.assertEqual({"A.ess", "A.skse"}, {Path(p).name for p in backed})
        self.assertEqual({"A.ess", "A.skse"}, set(unlocked))

    def test_turn_on_locks_every_folder_then_writes_and_locks_the_switch(self):
        cfg = self.d / "cfg" / "save-lock.json"
        f1, f2 = self.d / "f1", self.d / "f2"
        f1.mkdir(); f2.mkdir()
        self.pair("S", folder=f2)
        with patch.object(sl, "status_map", lambda folder, exts: {}):
            report = sl.turn_on(cfg, [f1, f2], EXTS)
        self.assertTrue(sl.read_config(cfg)["enabled"])
        self.assertEqual(("file", "save-lock.json"), self.calls[-1])
        self.assertIn(("folder", str(f1)), self.calls)
        self.assertIn(("folder", str(f2)), self.calls)
        self.assertEqual([(str(f1), 0, 0), (str(f2), 2, 0)], report)

    def test_a_folder_that_fails_leaves_the_switch_unset_and_says_what_it_locked(self):
        cfg = self.d / "cfg" / "save-lock.json"
        f1, f2 = self.d / "f1", self.d / "f2"
        f1.mkdir(); f2.mkdir()
        self.pair("S", folder=f1)

        def refuse_f2(p, sid):
            if str(p) == str(f2):
                raise OSError("no")
            self.calls.append(("folder", str(p)))
        with patch.object(sl, "status_map", lambda folder, exts: {}), patch.object(sl, "lock_folder", refuse_f2):
            with self.assertRaises(OSError) as raised:
                sl.turn_on(cfg, [f1, f2], EXTS)
        self.assertIn("NOT on", str(raised.exception))
        self.assertIn("2 save(s)", str(raised.exception))
        self.assertFalse(cfg.exists())
        self.assertNotIn(("file", "save-lock.json"), self.calls)

    def test_a_volume_without_permissions_refuses_before_any_rule_goes_on(self):
        cfg = self.d / "cfg" / "save-lock.json"
        f1 = self.d / "f1"
        f1.mkdir()
        with patch.object(sl, "has_acls", lambda p: False):
            with self.assertRaises(sl.Unsupported):
                sl.turn_on(cfg, [f1], EXTS)
        self.assertEqual([], self.calls)
        self.assertFalse(cfg.exists())

    def test_turn_on_is_refused_from_an_elevated_process(self):
        cfg = self.d / "save-lock.json"
        with patch.object(sl, "is_elevated", lambda: True):
            with self.assertRaises(PermissionError):
                sl.turn_on(cfg, [], EXTS)
        self.assertFalse(cfg.exists())

    def test_turn_off_needs_elevation(self):
        cfg = self.d / "save-lock.json"
        cfg.write_text('{"enabled": true}', encoding="utf-8")
        with self.assertRaises(PermissionError):
            sl.turn_off(cfg)
        self.assertTrue(sl.read_config(cfg)["enabled"])
        with patch.object(sl, "is_elevated", lambda: True), self.acls({}):
            sl.turn_off(cfg)
        self.assertFalse(sl.read_config(cfg)["enabled"])

    def off_world(self):
        """A switch, a locked save and a plain one in a locked folder, served through one DACL table."""
        cfg = self.d / "cfg" / "save-lock.json"
        cfg.parent.mkdir()
        cfg.write_text('{"enabled": true}', encoding="utf-8")
        self.pair("A"); self.pair("B")
        return cfg, {"A.ess": LOCKED, "save-lock.json": LOCKED, self.d.name: FOLDER_LOCKED}

    def test_turn_off_lifts_every_lock_it_finds(self):
        """The locks outlast the toolkit, so `off` is the way back to a plain folder."""
        cfg, table = self.off_world()
        lifted = []

        def unlock(p, sid):
            lifted.append(os.path.basename(str(p)))
            table.pop(os.path.basename(str(p)))
        with patch.object(sl, "is_elevated", lambda: True), self.acls(table), patch.object(sl, "unlock", unlock):
            self.assertEqual(2, sl.turn_off(cfg, [self.d], EXTS))
        self.assertEqual([self.d.name, "A.ess", "save-lock.json"], lifted)
        self.assertFalse(sl.read_config(cfg)["enabled"])

    def test_turn_off_keeps_the_switch_on_when_a_lock_cannot_be_lifted(self):
        cfg, table = self.off_world()

        def refuse(p, sid):
            raise PermissionError("unlock refused for " + str(p))
        with patch.object(sl, "is_elevated", lambda: True), self.acls(table), patch.object(sl, "unlock", refuse):
            with self.assertRaises(PermissionError) as raised:
                sl.turn_off(cfg, [self.d], EXTS)
        self.assertIn("stays ON", str(raised.exception))
        self.assertTrue(sl.read_config(cfg)["enabled"])

    def test_turn_off_stays_on_when_the_saves_read_locked_after_an_unlock_that_claimed_success(self):
        cfg, table = self.off_world()
        with patch.object(sl, "is_elevated", lambda: True), self.acls(table), \
                patch.object(sl, "unlock", lambda p, sid: None):
            with self.assertRaises(PermissionError) as raised:
                sl.turn_off(cfg, [self.d], EXTS)
        self.assertIn("still locked", str(raised.exception))
        self.assertTrue(sl.read_config(cfg)["enabled"])

    def test_turn_off_stays_on_when_a_save_cannot_be_read(self):
        cfg, table = self.off_world()

        def refuse(p):
            if str(p).endswith("A.ess"):
                raise PermissionError("denied")
            return PLAIN
        with patch.object(sl, "is_elevated", lambda: True), patch.object(sl, "_read_sddl", refuse), \
                patch.object(sl, "unlock", lambda p, sid: None):
            with self.assertRaises(PermissionError) as raised:
                sl.turn_off(cfg, [self.d], EXTS)
        self.assertIn("stays ON", str(raised.exception))
        self.assertTrue(sl.read_config(cfg)["enabled"])

    def test_turn_off_stays_on_when_the_folder_rule_cannot_be_read(self):
        cfg, table = self.off_world()

        def refuse(p):
            if str(p) == str(self.d):
                raise PermissionError("denied")
            return PLAIN
        with patch.object(sl, "is_elevated", lambda: True), patch.object(sl, "_read_sddl", refuse), \
                patch.object(sl, "unlock", lambda p, sid: None):
            with self.assertRaises(PermissionError):
                sl.turn_off(cfg, [self.d], EXTS)
        self.assertTrue(sl.read_config(cfg)["enabled"])

    def moved_world(self):
        cfg = self.d / "save-lock.json"
        cfg.write_text('{"enabled": true}', encoding="utf-8")
        folder = self.d / "save"
        folder.mkdir()
        sl._remember(cfg, folder)
        return cfg, folder

    def test_turn_off_stays_on_when_a_folder_it_locked_has_moved(self):
        cfg, folder = self.moved_world()
        folder.rename(self.d / "renamed")
        with patch.object(sl, "is_elevated", lambda: True), self.acls({}):
            with self.assertRaises(PermissionError) as raised:
                sl.turn_off(cfg, [], EXTS)
        self.assertIn("not where they were", str(raised.exception))
        self.assertIn(str(folder), str(raised.exception))
        self.assertTrue(sl.read_config(cfg)["enabled"])

    def test_turn_off_stays_on_when_a_new_folder_stands_at_the_old_path(self):
        cfg, folder = self.moved_world()
        folder.rename(self.d / "renamed")
        folder.mkdir()                                                          # the game makes a new one
        with patch.object(sl, "is_elevated", lambda: True), self.acls({}):
            with self.assertRaises(PermissionError):
                sl.turn_off(cfg, [folder], EXTS)
        self.assertTrue(sl.read_config(cfg)["enabled"])

    def test_TWIN_a_folder_where_it_was_is_swept_and_the_record_cleared(self):
        cfg, folder = self.moved_world()
        self.pair("A", folder=folder)
        table = {"A.ess": LOCKED}
        with patch.object(sl, "is_elevated", lambda: True), self.acls(table), \
                patch.object(sl, "unlock", lambda p, sid: table.pop(os.path.basename(str(p)))):
            self.assertEqual(1, sl.turn_off(cfg, [], EXTS))                     # found through the record alone
        self.assertFalse(sl.read_config(cfg)["enabled"])
        self.assertEqual(([], False), sl._read_record(cfg))

    # The same folder under two Pythons: 3.10 reports the volume serial's low 32 bits, 3.12 all 64 (measured 2026-11-03,
    # 3.10.10 and 3.12.10, one NTFS folder: st_ino equal, low 32 equal, st_dev not).
    FOLDER_32, FOLDER_64 = 0x8AF10FD2, 0xDC8A9F538AF10FD2

    def recorded_as(self, volume, file_id=77):
        cfg, folder = self.moved_world()
        sl._write_record(cfg, [[str(folder), volume, file_id]], False)
        return cfg, folder

    def off_seeing(self, cfg, folder, volume, file_id=77):
        with patch.object(sl, "is_elevated", lambda: True), self.acls({}), \
                patch.object(sl, "_identity", lambda f: [str(f), volume, file_id]):
            return sl.turn_off(cfg, [], EXTS)

    def test_a_record_from_another_python_is_the_same_folder_in_both_directions(self):
        for recorded, seen in ((self.FOLDER_32, self.FOLDER_64), (self.FOLDER_64, self.FOLDER_32)):
            cfg, folder = self.recorded_as(recorded)
            self.off_seeing(cfg, folder, seen)
            self.assertFalse(sl.read_config(cfg)["enabled"], (recorded, seen))
            self.assertEqual(([], False), sl._read_record(cfg))
            folder.rmdir()

    def test_TWIN_the_same_file_id_on_another_volume_is_not_the_same_folder(self):
        cfg, folder = self.recorded_as(self.FOLDER_64)
        with self.assertRaises(PermissionError):
            self.off_seeing(cfg, folder, self.FOLDER_64 ^ 1)
        self.assertTrue(sl.read_config(cfg)["enabled"])

    def test_TWIN_the_same_volume_with_another_file_id_is_not_the_same_folder(self):
        cfg, folder = self.recorded_as(self.FOLDER_32)
        with self.assertRaises(PermissionError):
            self.off_seeing(cfg, folder, self.FOLDER_64, file_id=78)
        self.assertTrue(sl.read_config(cfg)["enabled"])

    def test_TWIN_a_row_whose_volume_is_not_a_number_refuses_and_does_not_crash(self):
        cfg, folder = self.recorded_as("not a number")
        with self.assertRaises(PermissionError) as raised:
            self.off_seeing(cfg, folder, self.FOLDER_64)
        self.assertIn("not where they were", str(raised.exception))

    def test_looking_at_a_recorded_folder_from_another_python_adds_no_row(self):
        cfg, folder = self.recorded_as(self.FOLDER_32)
        with patch.object(sl, "_identity", lambda f: [str(f), self.FOLDER_64, 77]):
            sl._remember(cfg, folder)
        self.assertEqual(1, len(sl._read_record(cfg)[0]))

    def test_TWIN_a_new_folder_at_a_recorded_path_is_a_new_row(self):
        cfg, folder = self.recorded_as(self.FOLDER_32)
        with patch.object(sl, "_identity", lambda f: [str(f), self.FOLDER_64, 78]):
            sl._remember(cfg, folder)
        self.assertEqual(2, len(sl._read_record(cfg)[0]))

    def test_TWIN_the_same_folder_at_a_new_path_is_a_new_row(self):
        cfg, folder = self.recorded_as(self.FOLDER_32)
        with patch.object(sl, "_identity", lambda f: [str(self.d / "renamed"), self.FOLDER_32, 77]):
            sl._remember(cfg, folder)
        self.assertEqual(2, len(sl._read_record(cfg)[0]))

    def test_a_scanner_holding_the_record_for_a_moment_does_not_fail_the_write(self):
        cfg = self.d / "save-lock.json"
        real, calls = os.replace, []

        def held_once(a, b):
            calls.append(a)
            if len(calls) == 1:
                raise PermissionError("held")
            real(a, b)
        with patch.object(sl.os, "replace", held_once), patch.object(sl.time, "sleep", lambda s: None):
            sl._write_record(cfg, [["x", 1, 2]], False)
        self.assertEqual(2, len(calls))
        self.assertEqual(([["x", 1, 2]], False), sl._read_record(cfg))

    def test_TWIN_a_record_that_stays_held_raises_and_leaves_no_part_file(self):
        cfg = self.d / "save-lock.json"

        def held(a, b):
            raise PermissionError("held")
        with patch.object(sl.os, "replace", held), patch.object(sl.time, "sleep", lambda s: None):
            with self.assertRaises(PermissionError):
                sl._write_record(cfg, [["x", 1, 2]], False)
        self.assertEqual([], [f.name for f in self.d.iterdir() if f.name.startswith(".part-")])

    def test_a_record_that_cannot_be_written_still_says_what_was_locked_before_it(self):
        cfg = self.d / "save-lock.json"
        f1, f2 = self.d / "f1", self.d / "f2"
        f1.mkdir()
        f2.mkdir()
        self.pair("A", folder=f1)
        real = sl._remember

        def refuse_f2(config, folder):
            if Path(folder) == f2:
                raise OSError("record refused")
            real(config, folder)
        with patch.object(sl, "status_map", lambda folder, exts: {}), patch.object(sl, "_remember", refuse_f2):
            with self.assertRaises(OSError) as raised:
                sl.turn_on(cfg, [f1, f2], EXTS)
        self.assertIn("2 save(s) locked before this", str(raised.exception))
        self.assertIn("record refused", str(raised.exception))
        self.assertFalse(cfg.exists())

    def test_forget_missing_lets_off_finish_and_clears_the_record(self):
        cfg, folder = self.moved_world()
        folder.rename(self.d / "renamed")
        with patch.object(sl, "is_elevated", lambda: True), self.acls({}):
            sl.turn_off(cfg, [], EXTS, forget_missing=True)
        self.assertFalse(sl.read_config(cfg)["enabled"])
        self.assertEqual(([], False), sl._read_record(cfg))

    def test_an_unreadable_record_cannot_read_as_nothing_was_locked(self):
        cfg = self.d / "save-lock.json"
        cfg.write_text('{"enabled": true}', encoding="utf-8")
        sl._record_path(cfg).write_text("{not json", encoding="utf-8")
        with patch.object(sl, "is_elevated", lambda: True), self.acls({}):
            with self.assertRaises(PermissionError) as raised:
                sl.turn_off(cfg, [], EXTS)
        self.assertIn("could not be read", str(raised.exception))
        self.assertTrue(sl.read_config(cfg)["enabled"])

    def test_a_missing_or_broken_switch_means_off(self):
        self.assertEqual({"enabled": False}, sl.read_config(self.d / "none.json"))
        (self.d / "bad.json").write_text("{", encoding="utf-8")
        self.assertEqual({"enabled": False}, sl.read_config(self.d / "bad.json"))

    def test_auto_lock_locks_settled_saves_once_and_relocks_after_a_change(self):
        now = time.time()
        old = int((now - 3600) * 1e9)
        files = {"a": [1, old, str(self.d / "A.ess")], "r": [1, old, str(self.d / "Autosave1.ess")],
                 "f": [1, int(now * 1e9), str(self.d / "Fresh.ess")]}
        state = {}
        with patch.object(sl, "status_map", lambda folder, exts: {}):
            self.assertEqual([], sl.auto_lock(state, {}, files, [self.d], now=now))
            self.assertEqual([("folder", str(self.d)), ("file", "A.ess")], self.calls)
            self.calls.clear()
            sl.auto_lock(state, files, files, [self.d], now=now + 10)          # unchanged: nothing again
            self.assertEqual([], self.calls)
            changed = dict(files, a=[2, old + 5, str(self.d / "A.ess")])
            sl.auto_lock(state, files, changed, [self.d], now=now + 20)       # changed after an unlock
            self.assertEqual([("file", "A.ess")], self.calls)

    def test_auto_lock_rereads_the_real_locks_on_verify(self):
        now = time.time()
        old = int((now - 3600) * 1e9)
        path = str(self.d / "A.ess")
        files = {"a": [1, old, path]}
        with patch.object(sl, "status_map", lambda folder, exts: {path: True}):
            sl.auto_lock({}, {}, files, [self.d], now=now)
        self.assertNotIn(("file", "A.ess"), self.calls)                         # already locked on disk

    def test_auto_lock_reports_a_folder_rule_failure(self):
        def refuse(p, sid):
            raise PermissionError("icacls said no")
        now = time.time()
        with patch.object(sl, "status_map", lambda folder, exts: {}), patch.object(sl, "lock_folder", refuse):
            failed = sl.auto_lock({}, {}, {}, [self.d], now=now)
        self.assertEqual(1, len(failed))
        self.assertIn(str(self.d), failed[0])
        self.assertIn("icacls said no", failed[0])

    def test_auto_lock_reports_a_status_read_failure_and_retries_at_the_next_look(self):
        def refuse(folder, exts):
            raise PermissionError("cannot read")
        now = time.time()
        state = {}
        with patch.object(sl, "status_map", refuse):
            failed = sl.auto_lock(state, {}, {}, [self.d], now=now)
        self.assertTrue(failed and "cannot read" in failed[0])
        with patch.object(sl, "status_map", lambda folder, exts: {}):
            self.assertEqual([], sl.auto_lock(state, {}, {}, [self.d], now=now + 10))
        self.assertEqual(now + 10, state["lock_verified"])

    def test_a_new_folder_gets_its_rule_when_first_seen_not_at_the_hourly_verify(self):
        now = time.time()
        state = {}
        other = self.d / "new"
        other.mkdir()
        with patch.object(sl, "status_map", lambda folder, exts: {}):
            sl.auto_lock(state, {}, {}, [self.d], now=now)
            self.calls.clear()
            sl.auto_lock(state, {}, {}, [self.d, other], now=now + 10)          # inside the hour
            self.assertEqual([("folder", str(other))], self.calls)
            self.calls.clear()
            sl.auto_lock(state, {}, {}, [self.d, other], now=now + 20)          # once, not every look
            self.assertEqual([], self.calls)

    def test_auto_lock_records_each_folder_for_off(self):
        cfg = self.d / "save-lock.json"
        with patch.object(sl, "status_map", lambda folder, exts: {}):
            sl.auto_lock({}, {}, {}, [str(self.d)], now=time.time(), config=cfg)
        self.assertEqual([str(self.d)], [row[0] for row in sl._read_record(cfg)[0]])

    def test_TWIN_a_folder_already_carrying_its_rule_is_not_locked_again(self):
        with patch.object(sl, "status_map", lambda folder, exts: {}), patch.object(sl, "is_locked", lambda p: True):
            sl.auto_lock({}, {}, {}, [self.d], now=time.time())
        self.assertEqual([], self.calls)

    def test_off_windows_everything_is_unsupported(self):
        with patch.object(sl, "supported", lambda: False):
            for call in (lambda: sl.status_map(self.d, EXTS), lambda: sl.unlock("x", "s"), lambda: sl.turn_off("x"),
                         lambda: sl.turn_on(self.d / "c.json", [], EXTS), lambda: sl.auto_lock({}, {}, {}, [])):
                with self.assertRaises(sl.Unsupported):
                    call()


GUESTS = "S-1-5-32-546"     # a deny for Guests has the rule's shape but does not bind us, so the owner can clean up


@unittest.skipUnless(os.name == "nt", "real NTFS ACLs need Windows")
class RealAclTests(unittest.TestCase):
    """Real rules in a temp folder. Only shapes the test user can clean up: Guests' deny binds nobody here, and a real
    file rule still falls to the parent's delete-child right while the folder carries no rule of its own."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name) / "Jürgen сохр 存档"     # OEM cannot show these
        self.d.mkdir()
        self.f = self.d / "save_001.xml.gz"
        self.f.write_bytes(b"x")

    def icacls(self, path, *args):
        subprocess.run(["icacls", str(path), *args], check=True, capture_output=True)

    def as_guests(self):
        return patch.object(sl, "user_sid", lambda: GUESTS)

    def test_a_lock_on_a_non_ascii_path_reads_as_locked_and_a_plain_sibling_does_not(self):
        plain = self.d / "save_002.xml.gz"
        plain.write_bytes(b"x")
        sl.lock_file(self.f, GUESTS)
        with self.as_guests():
            self.assertTrue(sl.is_locked(self.f))
            self.assertFalse(sl.is_locked(plain))
            self.assertEqual({str(self.f): True, str(plain): False}, sl.status_map(self.d, (".xml.gz",)))

    def test_reading_never_runs_a_program_so_a_localized_listing_cannot_mislead_it(self):
        sl.lock_file(self.f, GUESTS)
        with self.as_guests(), patch.object(sl.subprocess, "run", side_effect=AssertionError("a read ran a program")):
            self.assertTrue(sl.is_locked(self.f))
            self.assertTrue(sl.status_map(self.d, (".xml.gz",))[str(self.f)])

    def test_a_lock_rule_under_another_accounts_sid_is_for_off_to_lift_and_does_not_protect_this_user(self):
        self.icacls(self.f, "/deny", "*{}:{}".format(GUESTS, sl.FILE_DENY))
        self.assertFalse(sl.is_plain(self.f))
        self.assertFalse(sl.is_locked(self.f))
        self.assertEqual({str(self.f): False}, sl.status_map(self.d, (".xml.gz",)))
        with self.as_guests():
            self.assertTrue(sl.is_locked(self.f))

    def test_locking_over_another_accounts_rule_really_holds_this_user(self):
        self.icacls(self.f, "/deny", "*{}:{}".format(GUESTS, sl.FILE_DENY))
        sl.lock_file(self.f, sl.user_sid())
        self.assertTrue(sl.is_locked(self.f))
        with self.assertRaises(PermissionError):
            open(self.f, "r+b").close()
        self.assertEqual(b"x", self.f.read_bytes())

    def test_TWIN_another_accounts_rule_does_not_satisfy_the_read_back(self):
        self.icacls(self.f, "/deny", "*{}:{}".format(GUESTS, sl.FILE_DENY))
        with patch.object(sl, "_icacls", lambda *a: (0, "ok")):
            with self.assertRaises(OSError):
                sl.lock_file(self.f, sl.user_sid())

    def test_a_folder_rule_under_another_accounts_sid_gets_this_users_rule_too(self):
        folder = Path(self.tmp.name) / "foreign"
        folder.mkdir()
        self.icacls(folder, "/deny", "*{}:{}".format(GUESTS, sl.FOLDER_DENY))
        self.assertFalse(sl.is_locked(folder))
        sl.lock_folder(folder, sl.user_sid())
        self.assertTrue(sl.is_locked(folder))

    def test_an_alias_resolves_through_windows_and_an_unknown_one_is_nobody(self):
        self.assertEqual(GUESTS, sl._alias_sid("BG"))
        self.assertIsNone(sl._alias_sid("not an alias"))

    def test_TWIN_someone_elses_deny_of_another_shape_is_not_our_lock(self):
        self.icacls(self.f, "/deny", "*{}:(W)".format(GUESTS))
        self.assertFalse(sl.is_locked(self.f))
        self.assertTrue(sl.is_plain(self.f))

    def test_TWIN_an_owner_rule_alone_is_not_a_lock_but_is_not_plain_either(self):
        self.icacls(self.f, "/grant", sl.OWNER_RIGHTS + ":(R)")
        self.assertFalse(sl.is_locked(self.f))
        self.assertFalse(sl.is_plain(self.f))

    def test_a_folder_rule_reads_as_locked(self):
        sl.lock_folder(self.d, GUESTS)
        with self.as_guests():
            self.assertTrue(sl.is_locked(self.d))

    def test_locking_a_locked_file_or_folder_again_does_not_raise(self):
        sid = sl.user_sid()
        sl.lock_file(self.f, sid)       # real: the rule refuses a second icacls, so only a read-first survives this
        sl.lock_file(self.f, sid)
        self.assertTrue(sl.is_locked(self.f))
        other = Path(self.tmp.name) / "folder"
        other.mkdir()
        sl.lock_folder(other, sid)
        sl.lock_folder(other, sid)
        self.assertTrue(sl.is_locked(other))

    def test_a_path_that_cannot_be_read_raises_and_never_reads_as_not_locked(self):
        with self.assertRaises(OSError):
            sl.is_locked(self.d / "missing.xml.gz")

    def test_off_refuses_and_stays_on_while_a_non_ascii_save_is_still_locked(self):
        config = self.d / "save-lock.json"
        config.write_text('{"enabled": true}', encoding="utf-8")
        sl.lock_file(self.f, GUESTS)

        def not_elevated():
            raise PermissionError("not elevated")
        with patch.object(sl, "is_elevated", lambda: True), patch.object(sl, "_enable_restore_privilege", not_elevated):
            with self.assertRaises(PermissionError) as raised:
                sl.turn_off(config, [self.d], (".xml.gz",))
        self.assertIn("stays ON", str(raised.exception))
        self.assertTrue(sl.read_config(config)["enabled"])

    def test_the_sid_comes_from_the_process_token_not_a_program_on_path(self):
        oracle = subprocess.run([os.path.join(os.environ["SystemRoot"], "System32", "whoami.exe"), "/user", "/fo", "csv",
                                 "/nh"], capture_output=True, check=True).stdout.decode("ascii", "replace")
        expected = oracle.strip().rsplit(",", 1)[1].strip('"')
        with patch.object(sl.subprocess, "run", side_effect=FileNotFoundError("whoami")):
            self.assertEqual(expected, sl.user_sid())

    def test_icacls_is_run_by_full_path(self):
        seen = []

        def run(args, **kw):
            seen.append(args[0])
            return subprocess.CompletedProcess(args, 1, "", "no")
        with patch.object(sl.subprocess, "run", run):
            with self.assertRaises(OSError):
                sl.lock_file(self.f, GUESTS)
        self.assertTrue(os.path.isabs(seen[0]) and seen[0].lower().endswith("icacls.exe"), seen)

    def test_a_real_rule_binds_and_only_a_rename_of_its_folder_gets_through(self):
        """The docstring's measured claim. The parent folder carries no rule, so the folder itself still moves."""
        sid = sl.user_sid()
        folder = Path(self.tmp.name) / "ruled"
        folder.mkdir()
        sl.lock_folder(folder, sid)
        moved = Path(self.tmp.name) / "moved"
        os.rename(folder, moved)
        self.assertTrue(sl.is_locked(moved))
        sl.lock_file(self.f, sid)
        with self.assertRaises(PermissionError):
            open(self.f, "r+b").close()
        self.assertEqual(b"x", self.f.read_bytes())

    def test_a_lock_rule_comes_off_for_real_through_the_dacl_rewrite_and_reads_plain(self):
        """The restore privilege is the only stand-in: the owner may rewrite a DACL whose deny names Guests, so the
        rewrite, the strip of a rule naming another account, and the read-back are all real."""
        sl.lock_file(self.f, GUESTS)
        with patch.object(sl, "_enable_restore_privilege", lambda: None):
            sl.unlock(self.f, sl.user_sid())
        self.assertTrue(sl.is_plain(self.f))
        self.f.write_bytes(b"y")

    def test_on_then_off_for_real_round_trips_a_non_ascii_folder(self):
        """Every read, write and record is real; the rules name Guests so they bind nobody and need no elevation."""
        config = Path(self.tmp.name) / "state" / "save-lock.json"
        old = time.time() - 3600
        os.utime(self.f, (old, old))
        with self.as_guests(), patch.object(sl, "is_elevated", lambda: False):
            report = sl.turn_on(config, [self.d], (".xml.gz",))
            self.assertTrue(sl.is_locked(self.f) and sl.is_locked(self.d) and sl.is_locked(config))
            self.assertEqual({str(self.f): True}, sl.status_map(self.d, (".xml.gz",)))
        self.assertEqual([(str(self.d), 1, 0)], report)
        self.assertTrue(sl.read_config(config)["enabled"])
        with patch.object(sl, "is_elevated", lambda: True), patch.object(sl, "_enable_restore_privilege", lambda: None):
            self.assertEqual(2, sl.turn_off(config, [], (".xml.gz",)))          # found through the record alone
        self.assertTrue(sl.is_plain(self.f) and sl.is_plain(self.d) and sl.is_plain(config))
        self.assertFalse(sl.read_config(config)["enabled"])

    def test_this_temp_volume_keeps_permissions(self):
        self.assertTrue(sl.has_acls(self.d))

    def test_lock_file_refuses_when_icacls_says_ok_but_no_rule_arrived(self):
        with patch.object(sl, "_icacls", lambda *a: (0, "ok")):
            with self.assertRaises(OSError):
                sl.lock_file(self.f, GUESTS)


if __name__ == "__main__":
    unittest.main()
