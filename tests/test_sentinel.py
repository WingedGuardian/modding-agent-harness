import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modding_harness import sentinel
from modding_harness.preservation import SaveStore
from modding_harness.profiles import skyrim, x4


class SentinelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.saves = self.root / "Saves"
        self.saves.mkdir()
        self.profile = skyrim([self.saves])
        self.store = SaveStore(self.root, destination=self.root / "store", save_roots=[self.saves], reserve=0)
        self.state = {}
        self.clock = 1_000_000.0

    def pair(self, folder, stem, data=b"E"):
        for ext in (".ess", ".skse"):
            (folder / (stem + ext)).write_bytes(data)
        return folder / (stem + ".ess")

    def bump(self, path, data):
        path.write_bytes(data)
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))

    def look(self, watch=()):
        self.clock += 10
        return sentinel.check(self.profile, self.store, self.state, watch=watch, now=self.clock)

    def test_first_look_is_a_baseline_and_new_saves_are_not_changes(self):
        self.pair(self.saves, "A")
        self.assertTrue(self.look().first)
        self.pair(self.saves, "B")
        report = self.look()
        self.assertEqual((False, (), ()), (report.first, report.unbacked, report.backed_up))

    def test_modified_and_removed_members_without_a_backup_are_reported(self):
        a = self.pair(self.saves, "A")
        self.pair(self.saves, "B")
        self.look()
        self.bump(a, b"changed")
        (self.saves / "B.skse").unlink()
        report = self.look()
        self.assertEqual({("modified", str(a)), ("removed", str(self.saves / "B.skse"))}, set(report.unbacked))
        self.assertIn("no backup taken first", sentinel.describe(report))
        self.assertEqual((), self.look().unbacked)      # reported once; the new look is the baseline

    def test_a_committed_backup_since_the_last_look_covers_the_change(self):
        a = self.pair(self.saves, "A")
        self.clock = __import__("time").time() - 60
        self.look()
        self.store.backup([self.profile.members(a)])
        self.bump(a, b"changed")
        self.clock = __import__("time").time()
        report = sentinel.check(self.profile, self.store, self.state, now=self.clock)
        self.assertEqual((), report.unbacked)
        self.assertEqual((("modified", str(a)),), report.backed_up)

    def test_an_old_backup_does_not_cover_a_new_change(self):
        a = self.pair(self.saves, "A")
        self.store.backup([self.profile.members(a)])
        old = self.store.root / "transactions"
        for p in old.glob("*.json"):
            os.utime(p, (1, 1))
        self.clock = __import__("time").time() + 3600
        self.look()
        self.bump(a, b"changed")
        self.assertEqual((("modified", str(a)),), self.look().unbacked)

    def test_a_fresh_file_holding_an_old_transaction_does_not_cover(self):
        # The mtime pre-filter and the recorded time are separate clauses: a copied store has fresh files.
        a = self.pair(self.saves, "A")
        self.store.backup([self.profile.members(a)])
        for p in (self.store.root / "transactions").glob("*.json"):
            m = json.loads(p.read_text(encoding="ascii"))
            m["utc"] = "2001-01-01T00:00:00+00:00"
            p.write_text(json.dumps(m), encoding="ascii")
        self.clock = __import__("time").time() - 100     # the previous look precedes the fresh file's mtime
        self.look()
        self.bump(a, b"changed")
        self.assertEqual((("modified", str(a)),), self.look().unbacked)

    def test_caller_supplied_legacy_records_cover_too(self):
        a = self.pair(self.saves, "A")
        self.look()
        self.bump(a, b"changed")
        key = sentinel.comparison_key(os.path.abspath(a))
        self.clock += 10
        report = sentinel.check(self.profile, self.store, self.state, now=self.clock, covered={key})
        self.assertEqual((), report.unbacked)

    def test_watched_folders_are_walked_and_capped(self):
        kept = self.root / "kept" / "deep"
        kept.mkdir(parents=True)
        b = self.pair(kept, "B")
        self.look(watch=[self.root / "kept"])
        b.unlink()
        self.assertEqual((("removed", str(b)),), self.look(watch=[self.root / "kept"]).unbacked)
        for i in range(5):
            (kept / ("n{}.txt".format(i))).write_bytes(b"")
        files, truncated = sentinel.snapshot([], (".ess",), watch=[self.root / "kept"], limit=3)
        self.assertEqual((str(self.root / "kept"),), truncated)

    def test_profile_extensions_decide_membership(self):
        profile = x4([self.saves])
        (self.saves / "save_001.xml.gz").write_bytes(b"x")
        (self.saves / "A.ess").write_bytes(b"x")
        files, _ = sentinel.snapshot(profile.save_roots, profile.extensions)
        self.assertEqual(["save_001.xml.gz"], [Path(v[2]).name for v in files.values()])

    def test_state_round_trips_and_a_bad_state_is_a_baseline(self):
        path = self.root / "diag" / "sentinel.json"
        self.pair(self.saves, "A")
        self.look()
        sentinel.save_state(path, self.state)
        self.assertEqual(self.state["files"], sentinel.load_state(path)["files"])
        self.assertEqual([], [p.name for p in path.parent.iterdir() if p.name.startswith(".part-")])
        path.write_text("{not json", encoding="utf-8")
        self.assertNotIn("files", sentinel.load_state(path))
        path.write_text(json.dumps({"v": 99}), encoding="utf-8")
        self.assertNotIn("files", sentinel.load_state(path))

    def test_an_unreadable_state_file_is_disclosed_never_a_silent_baseline(self):
        path = self.root / "diag" / "sentinel.json"
        path.parent.mkdir()
        self.pair(self.saves, "A")
        path.write_text("{not json", encoding="utf-8")
        state = sentinel.load_state(path)
        report = sentinel.check(self.profile, self.store, state, now=self.clock)
        self.assertTrue(report.first and report.state_lost)
        self.assertIn("could not be read", sentinel.describe(report))
        self.assertNotIn("unreadable", state)               # the disclosure is one look's, not carried on

    def test_TWIN_a_missing_state_file_is_a_plain_first_look(self):
        state = sentinel.load_state(self.root / "none.json")
        report = sentinel.check(self.profile, self.store, state, now=self.clock)
        self.assertTrue(report.first)
        self.assertFalse(report.state_lost)
        self.assertEqual("", sentinel.describe(report))

    def test_a_deleted_state_file_is_disclosed_never_a_silent_baseline(self):
        path = self.root / "diag" / "sentinel.json"
        self.pair(self.saves, "A")
        self.look()
        sentinel.save_state(path, self.state)
        path.unlink()
        state = sentinel.load_state(path)
        report = sentinel.check(self.profile, self.store, state, now=self.clock)
        self.assertTrue(report.first and report.state_lost)
        self.assertIn("gone or could not be read", sentinel.describe(report))
        sentinel.save_state(path, state)                    # the disclosure is one look's
        self.assertFalse(sentinel.check(self.profile, self.store, sentinel.load_state(path), now=self.clock).state_lost)

    def test_TWIN_a_save_state_failure_cannot_leave_the_mark_behind_without_a_record(self):
        path = self.root / "diag" / "sentinel.json"
        with patch.object(sentinel.os, "replace", side_effect=OSError("no")):
            with self.assertRaises(OSError):
                sentinel.save_state(path, {"v": 1, "files": {}})
        self.assertFalse(sentinel.load_state(path).get("unreadable"))

    # R3-I1: the record is shared by every session and hooks run in parallel, so a read or a replace can meet a lock.

    def recorded(self, name="sentinel.json"):
        path = self.root / "diag" / name
        self.pair(self.saves, "A")
        self.look()
        sentinel.save_state(path, self.state)
        return path

    def held_reads(self, path, times=None):
        """Path.read_text on the record raising PermissionError `times` times (None: always), as during another look's
        os.replace; every other read goes through. Also returns the list that counts the tries on the record."""
        real, tries = Path.read_text, []

        def read_text(this, *args, **kwargs):
            if this == path:
                tries.append(1)
                if times is None or len(tries) <= times:
                    raise PermissionError(13, "held", str(path))
            return real(this, *args, **kwargs)
        return patch.object(Path, "read_text", read_text), tries

    def test_a_read_that_meets_a_lock_is_retried_and_finds_the_record(self):
        path = self.recorded()
        patched, tries = self.held_reads(path, times=2)
        with patched, patch.object(sentinel.time, "sleep") as sleep:
            state = sentinel.load_state(path)
        self.assertEqual(self.state["files"], state["files"])
        self.assertEqual(3, len(tries))
        self.assertEqual(2, sleep.call_count)

    def test_TWIN_the_common_read_tries_once_and_never_waits(self):
        path = self.recorded()
        patched, tries = self.held_reads(path, times=0)
        with patched, patch.object(sentinel.time, "sleep") as sleep:
            sentinel.load_state(path)
        self.assertEqual((1, 0), (len(tries), sleep.call_count))

    def test_a_record_still_held_after_the_retries_keeps_its_baseline_and_the_change_is_reported_next_look(self):
        path = self.recorded()
        a = self.saves / "A.ess"
        self.bump(a, b"changed while the record was held")
        before = path.read_bytes()
        patched, tries = self.held_reads(path)
        with patched, patch.object(sentinel.time, "sleep") as sleep:
            state = sentinel.load_state(path, now=self.clock)
        self.assertEqual(len(sentinel.REPLACE_WAITS), len(tries), "bounded by the same waits as save_lock")
        self.assertLess(sum(c.args[0] for c in sleep.call_args_list), 1.0, "one look waits under a second")
        report = sentinel.check(self.profile, self.store, state, now=self.clock + 10)
        self.assertEqual((False, False, (), True), (report.first, report.state_lost, report.unbacked, report.record_held))
        self.assertIn("held by another process", sentinel.describe(report))
        self.assertNotIn("could not be read", sentinel.describe(report))
        sentinel.save_state(path, state)
        self.assertEqual(before, path.read_bytes(), "a look that read nothing must not write over the baseline")
        again = sentinel.check(self.profile, self.store, sentinel.load_state(path), now=self.clock + 20)
        self.assertEqual((("modified", str(a)),), again.unbacked)
        self.assertFalse(again.state_lost)

    def test_a_record_held_past_the_window_is_lost_and_says_why(self):
        path = self.recorded()
        patched, _tries = self.held_reads(path)
        with patched, patch.object(sentinel.time, "sleep"):
            first = sentinel.load_state(path, now=5000.0)
            early = sentinel.load_state(path, now=5000.0 + sentinel.STUCK_AFTER - 1)
            late = sentinel.load_state(path, now=5000.0 + sentinel.STUCK_AFTER)
            soon_after = sentinel.load_state(path, now=5000.0 + sentinel.STUCK_AFTER + 1)
        self.assertTrue(first["busy"] and early["busy"], "under the window it is one more busy look")
        self.assertTrue(late["unreadable"])
        self.assertTrue(soon_after["busy"], "a record that stays held alarms once per window, not on every tool call")
        report = sentinel.check(self.profile, self.store, late, now=self.clock + 10)
        self.assertTrue(report.state_lost and report.first and not report.record_held)
        self.assertIn("could not be read", sentinel.describe(report))
        self.assertIn("stayed locked", sentinel.describe(report))

    def test_TWIN_a_recorded_look_clears_the_held_stamp(self):
        path = self.recorded()
        patched, _tries = self.held_reads(path)
        with patched, patch.object(sentinel.time, "sleep"):
            self.assertTrue(sentinel.load_state(path, now=5000.0)["busy"])
        sentinel.save_state(path, sentinel.load_state(path))    # a look got through between the two failures
        with patched, patch.object(sentinel.time, "sleep"):
            state = sentinel.load_state(path, now=5000.0 + sentinel.STUCK_AFTER * 10)
        self.assertTrue(state["busy"], "two transient locks a long way apart are not one stuck record")

    def test_TWIN_a_corrupt_record_is_lost_at_once_with_no_wait(self):
        path = self.recorded()
        path.write_text("{not json", encoding="utf-8")
        with patch.object(sentinel.time, "sleep") as sleep:
            state = sentinel.load_state(path)
        report = sentinel.check(self.profile, self.store, state, now=self.clock + 10)
        self.assertEqual(0, sleep.call_count)
        self.assertTrue(report.state_lost and report.first and not report.record_held)
        self.assertIn("gone or could not be read", sentinel.describe(report))
        self.assertIn("corrupt", sentinel.describe(report))

    def test_TWIN_a_deleted_record_is_lost_at_once_with_no_wait(self):
        path = self.recorded()
        path.unlink()
        with patch.object(sentinel.time, "sleep") as sleep:
            state = sentinel.load_state(path)
        report = sentinel.check(self.profile, self.store, state, now=self.clock + 10)
        self.assertEqual(0, sleep.call_count)
        self.assertTrue(report.state_lost and report.first and not report.record_held)
        self.assertIn("gone or could not be read", sentinel.describe(report))
        self.assertIn("deleted", sentinel.describe(report))

    def test_TWIN_another_read_failure_is_lost_at_once_not_waited_on(self):
        path = self.recorded()
        with patch.object(Path, "read_text", side_effect=OSError(5, "input/output error")), \
                patch.object(sentinel.time, "sleep") as sleep:
            state = sentinel.load_state(path)
        self.assertEqual(0, sleep.call_count)
        self.assertTrue(state["unreadable"])
        self.assertNotIn("busy", state)

    def test_a_replace_that_meets_a_lock_is_retried(self):
        path = self.root / "diag" / "sentinel.json"
        real, tries = os.replace, []

        def replace(a, b):
            tries.append(1)
            if len(tries) <= 2:
                raise PermissionError(13, "held")
            return real(a, b)
        with patch.object(sentinel.os, "replace", replace), patch.object(sentinel.time, "sleep") as sleep:
            sentinel.save_state(path, {"v": 1, "files": {}})
        self.assertEqual((3, 2), (len(tries), sleep.call_count))
        self.assertEqual({"v": 1, "files": {}}, sentinel.load_state(path))
        self.assertEqual([], [p.name for p in path.parent.iterdir() if p.name.startswith(".part-")])

    def test_TWIN_a_replace_held_past_the_retries_raises_and_leaves_no_scratch(self):
        path = self.root / "diag" / "sentinel.json"
        with patch.object(sentinel.os, "replace", side_effect=PermissionError(13, "held")) as replace, \
                patch.object(sentinel.time, "sleep"):
            with self.assertRaises(PermissionError):
                sentinel.save_state(path, {"v": 1, "files": {}})
        self.assertEqual(len(sentinel.REPLACE_WAITS), replace.call_count)
        self.assertEqual([], [p.name for p in path.parent.iterdir()])

    def test_TWIN_the_common_replace_tries_once_and_another_failure_is_not_retried(self):
        path = self.root / "diag" / "sentinel.json"
        with patch.object(sentinel.time, "sleep") as sleep:
            sentinel.save_state(path, {"v": 1, "files": {}})
        self.assertEqual(0, sleep.call_count)
        with patch.object(sentinel.os, "replace", side_effect=OSError(5, "no")) as replace, \
                patch.object(sentinel.time, "sleep") as sleep:
            with self.assertRaises(OSError):
                sentinel.save_state(path, {"v": 1, "files": {}})
        self.assertEqual((1, 0), (replace.call_count, sleep.call_count))

    def test_a_root_that_cannot_be_listed_reports_no_removals_and_says_so(self):
        a = self.pair(self.saves, "A")
        self.look()
        real = os.scandir

        def refuse(path):
            if Path(path) == self.saves:
                raise PermissionError("denied")
            return real(path)
        with patch.object(sentinel.os, "scandir", refuse):
            report = self.look()
        self.assertEqual((), report.unbacked)
        self.assertEqual((str(self.saves),), report.unread)
        self.assertIn("could not be listed", sentinel.describe(report))
        a.unlink()                                          # the carried row still catches a real removal later
        self.assertEqual((("removed", str(a)),), self.look().unbacked)

    def test_TWIN_a_readable_empty_root_is_a_real_removal(self):
        a = self.pair(self.saves, "A")
        self.look()
        a.unlink()
        (self.saves / "A.skse").unlink()
        self.assertEqual({("removed", str(a)), ("removed", str(self.saves / "A.skse"))}, set(self.look().unbacked))

    def test_TWIN_a_save_folder_that_is_gone_is_a_real_removal(self):
        a = self.pair(self.saves, "A")
        self.look()
        a.unlink()
        (self.saves / "A.skse").unlink()
        self.saves.rmdir()
        report = self.look()
        self.assertEqual({("removed", str(a)), ("removed", str(self.saves / "A.skse"))}, set(report.unbacked))
        self.assertEqual((), report.unread)

    def test_a_watched_folder_over_the_walk_limit_reports_no_removals(self):
        kept = self.root / "kept"
        for stem in ("A", "B", "C", "D"):
            folder = kept / stem
            folder.mkdir(parents=True)
            self.pair(folder, stem)                         # one folder per stem: the walk stops before the later ones
        self.look(watch=[kept])
        with patch.object(sentinel, "WALK_LIMIT", 1):
            report = self.look(watch=[kept])
        self.assertEqual((), report.unbacked)
        self.assertEqual((str(kept),), report.truncated)

    def test_TWIN_a_folder_under_the_limit_still_reports_its_removals(self):
        kept = self.root / "kept"
        kept.mkdir()
        b = self.pair(kept, "B")
        self.look(watch=[kept])
        b.unlink()
        self.assertEqual((("removed", str(b)),), self.look(watch=[kept]).unbacked)

    def test_a_change_dated_inside_a_session_window_is_inside_it(self):
        a = self.pair(self.saves, "A")
        os.utime(a, (5000.0, 5000.0))
        self.assertTrue(sentinel.dated_within("modified", str(a), 4000.0, 6000.0))
        self.assertTrue(sentinel.dated_within("modified", str(a), 4000.0, None))         # no end: still running

    def test_TWIN_a_change_dated_after_the_session_or_before_the_last_look_is_outside(self):
        a = self.pair(self.saves, "A")
        os.utime(a, (5000.0, 5000.0))
        self.assertFalse(sentinel.dated_within("modified", str(a), 4000.0, 4900.0))      # written after the game left
        self.assertFalse(sentinel.dated_within("modified", str(a), 5100.0, 6000.0))      # old stamp: restored, not saved
        self.assertFalse(sentinel.dated_within("modified", str(self.saves / "gone"), 4000.0, 6000.0))    # cannot date

    def test_a_removal_is_dated_by_its_folder(self):
        os.utime(self.saves, (5000.0, 5000.0))
        gone = str(self.saves / "gone.ess")
        self.assertTrue(sentinel.dated_within("removed", gone, 4000.0, 6000.0))
        self.assertFalse(sentinel.dated_within("removed", gone, 4000.0, 4900.0))
        self.assertFalse(sentinel.dated_within("removed", str(self.root / "no" / "gone.ess"), 4000.0, 6000.0))


class OpusReviewTests(unittest.TestCase):
    def test_a_file_gone_between_listing_and_stat_loses_only_itself(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ("a.ess", "b.ess", "c.ess"):
                Path(folder, name).write_bytes(b"x")
            real = os.scandir

            class Entry:
                def __init__(self, e):
                    self.e, self.name, self.path = e, e.name, e.path

                def is_file(self):
                    return True

                def stat(self):
                    if self.name == "b.ess":
                        raise FileNotFoundError(self.path)
                    return self.e.stat()

            class Listing:
                def __init__(self, path):
                    self.items = [Entry(e) for e in sorted(real(path), key=lambda e: e.name)]

                def __enter__(self):
                    return iter(self.items)

                def __exit__(self, *a):
                    return False
            with patch.object(sentinel.os, "scandir", Listing):
                files, _truncated, unread = sentinel._scan([folder], (".ess",), (), 100)
            self.assertEqual(sorted(os.path.basename(v[2]) for v in files.values()), ["a.ess", "c.ess"])
            self.assertEqual(unread, ())


if __name__ == "__main__":
    unittest.main()
