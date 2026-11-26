"""POSIX: a path longer than any PATH_MAX is resolved lexically, in one pass (issue #16)."""
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from modding_harness import profiles

LIMIT = profiles._POSIX_PATH_MAX


def repeated(n):
    """The issue's shape: a repeated directory spelling that does not exist."""
    return ("/x/install/game/" * (n // 16 + 1))[:n]


def no_resolve(self, strict=False):
    raise AssertionError("Path.resolve must not run for a path the OS refuses: " + str(self)[:40])


@unittest.skipIf(os.name == "nt", "POSIX resolver; Windows keeps its own bounded branch")
class PosixLongPathTests(unittest.TestCase):
    def test_limit_is_the_longest_posix_path_max(self):
        self.assertEqual(4096, LIMIT)

    def test_over_the_limit_is_lexical_and_never_resolved(self):
        target = repeated(LIMIT + 1)
        with patch.object(Path, "resolve", no_resolve):
            result = profiles.canonical(target)
        self.assertEqual(os.path.normcase(os.path.normpath(target)), result)

    def test_at_the_limit_resolution_stays_native(self):
        target = repeated(LIMIT)
        calls = []
        real = Path.resolve

        def resolve(self, strict=False):
            calls.append(strict)
            return real(self, strict=strict)

        with patch.object(Path, "resolve", resolve):
            result = profiles.canonical(target)
        self.assertEqual([False], calls)
        self.assertEqual(profiles.comparison_key(str(Path(target).resolve())), result)

    def test_dot_parents_collapse_as_realpath_collapses_them(self):
        target = "/a/" + "b/" * (LIMIT // 2) + "../" * (LIMIT // 2) + "c"
        self.assertGreater(len(target), LIMIT)
        with patch.object(Path, "resolve", no_resolve):
            self.assertEqual(os.path.normcase("/a/c"), profiles.canonical(target))

    def test_a_relative_over_length_path_is_taken_from_the_cwd(self):
        target = "rel/" * (LIMIT // 4 + 1)
        with patch.object(Path, "resolve", no_resolve):
            result = profiles.canonical(target)
        self.assertEqual(os.path.normcase(os.path.normpath(os.path.join(os.getcwd(), target))), result)

    def test_cost_grows_linearly_past_the_limit(self):
        # Quadratic doubles to ~4x per doubling of length; linear to ~2x. The bound is loose
        # so a busy runner cannot fail it, and still far below the issue's measured 3-4x.
        def cost(n):
            target = repeated(n)
            best = None
            for _ in range(5):
                started = time.process_time()
                for _ in range(20):
                    profiles.canonical(target)
                spent = time.process_time() - started
                best = spent if best is None else min(best, spent)
            return max(best, 1e-6)

        small, large = cost(40_000), cost(160_000)
        self.assertLess(large / small, 10, "4x the length cost {:.1f}x".format(large / small))


if __name__ == "__main__":
    unittest.main()
