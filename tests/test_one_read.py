import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from modding_harness import preservation
from modding_harness.preservation import contains, one_read, protects


class OneReadTests(unittest.TestCase):
    """A bulk check re-resolved the same paths about 18 times each and spent its deadline on it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = self.root / "store"
        self.store.mkdir()
        real = preservation.canonical
        self.calls = []

        def counted(path):
            self.calls.append(os.fspath(path))
            return real(path)

        patcher = patch.object(preservation, "canonical", counted)
        patcher.start()
        self.addCleanup(patcher.stop)

    def outside(self, i):
        return self.root / "dev" / "ship-{}.xml".format(i)

    def test_each_spelling_resolves_once_inside_one_read(self):
        with one_read():
            for _ in range(5):
                for i in range(3):
                    protects(self.outside(i), [self.store])
                    contains(self.store, self.outside(i))
        self.assertEqual(sorted(self.calls), sorted([os.fspath(self.store)] + [os.fspath(self.outside(i)) for i in range(3)]))

    def test_nothing_is_remembered_outside_one_read(self):
        contains(self.store, self.outside(0))
        contains(self.store, self.outside(0))
        self.assertEqual(len(self.calls), 4)

    def test_nothing_is_remembered_between_reads(self):
        for _ in range(2):
            with one_read():
                contains(self.store, self.outside(0))
        self.assertEqual(len(self.calls), 4)

    def test_memo_ends_when_the_read_raises(self):
        with self.assertRaises(TimeoutError):
            with one_read():
                contains(self.store, self.outside(0))
                raise TimeoutError
        self.calls.clear()
        contains(self.store, self.outside(0))
        self.assertEqual(len(self.calls), 2)

    def test_a_nested_read_restores_the_outer_memo(self):
        with one_read():
            contains(self.store, self.outside(0))
            self.calls.clear()
            with one_read():
                contains(self.store, self.outside(0))
            self.assertEqual(len(self.calls), 2, "a nested read starts fresh")
            self.calls.clear()
            contains(self.store, self.outside(0))
        self.assertEqual(self.calls, [])

    def test_another_thread_never_sees_this_threads_memo(self):
        seen = []

        def other():
            before = len(self.calls)
            contains(self.store, self.outside(0))
            seen.append(len(self.calls) - before)

        with one_read():
            contains(self.store, self.outside(0))
            worker = threading.Thread(target=other)
            worker.start()
            worker.join()
        self.assertEqual(seen, [2])

    def test_verdicts_stay_distinct_inside_one_read(self):
        # Same parent, same basename, and a store path: a coarse key would merge these.
        twin = self.root / "elsewhere" / "ship-0.xml"
        inside = self.store / "ship-0.xml"
        sibling = self.root / "storefront.xml"
        with one_read():
            self.assertTrue(protects(inside, [self.store]))
            self.assertFalse(protects(twin, [self.store]))
            self.assertFalse(protects(sibling, [self.store]))
            self.assertTrue(contains(self.store, inside))
            self.assertFalse(contains(self.store, twin))
            self.assertTrue(protects(self.root, [self.store], ancestor=True))
            self.assertFalse(protects(self.root, [self.store], ancestor=False))

    def test_a_failed_resolution_is_not_remembered(self):
        with patch.object(preservation, "canonical", side_effect=[OSError("first"), "ok"]) as failing:
            with one_read():
                with self.assertRaises(OSError):
                    preservation._key("x")
                self.assertEqual(preservation._key("x"), "ok")
        self.assertEqual(failing.call_count, 2)


if __name__ == "__main__":
    unittest.main()
