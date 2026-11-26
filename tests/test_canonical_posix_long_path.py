"""POSIX: long paths resolve natively in time linear in a missing tail (issue #16).

The oracle is the native resolver itself: every shape must give exactly what
`Path.resolve()` gives, symlinks in the existing prefix included, because the
preservation guards compare these keys against protected roots.
"""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from modding_harness import preservation, profiles


def repeated(n):
    """The issue's shape: a repeated directory spelling that does not exist."""
    return ("/x/install/game/" * (n // 16 + 1))[:n]


@unittest.skipIf(os.name == "nt", "POSIX resolver; Windows keeps its own bounded branch")
class PosixLongPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.saves = self.root / "real" / "saves"
        self.saves.mkdir(parents=True)
        (self.root / "savelink").symlink_to(self.saves)
        (self.root / "dangling").symlink_to(self.root / "nowhere")
        (self.root / "afile").write_text("x")
        deep = self.root / "deep" / Path(*(["d"] * 40))
        deep.mkdir(parents=True)
        self.deep = deep
        # A symlink whose TARGET holds `missing/..`: realpath walks past it into `real`, where
        # `sav` leads on into the protected root (review finding 2).
        (self.root / "t" / "real").mkdir(parents=True)
        (self.root / "t" / "real" / "sav").symlink_to(self.saves)
        (self.root / "L").symlink_to(str(self.root / "t" / "nowhere" / ".." / "real"))
        # A real existing tree past PATH_MAX, built one level at a time through dir_fd.
        self.over = self.root / "over"
        self.over.mkdir()
        fd = os.open(self.over, os.O_RDONLY)
        try:
            for _ in range(25):                       # 25 x 201 characters > 4096
                os.mkdir("p" * 200, dir_fd=fd)
                child = os.open("p" * 200, os.O_RDONLY, dir_fd=fd)
                os.close(fd)
                fd = child
        finally:
            os.close(fd)

        def remove_over():
            # TemporaryDirectory cleanup cannot remove a tree past PATH_MAX by full paths.
            def walk(fd, depth):
                if depth < 25:
                    child = os.open("p" * 200, os.O_RDONLY, dir_fd=fd)
                    try:
                        walk(child, depth + 1)
                    finally:
                        os.close(child)
                    os.rmdir("p" * 200, dir_fd=fd)
            top = os.open(self.over, os.O_RDONLY)
            try:
                walk(top, 0)
            finally:
                os.close(top)
        self.addCleanup(remove_over)

    def same_as_native(self, value):
        expected = profiles.comparison_key(str(Path(value).resolve()))
        self.assertEqual(expected, profiles.canonical(value), str(value)[:120])

    def test_every_shape_equals_native_resolution(self):
        tail = Path(*(["m"] * 600))
        shapes = {
            "missing from the root": repeated(20_000),
            "symlink then long missing tail": self.root / "savelink" / tail / "s.ess",
            "dangling symlink then tail": self.root / "dangling" / tail,
            "file used as a folder": self.root / "afile" / tail,
            "deep existing prefix then tail": self.deep / tail,
            "fully existing deep path": self.deep,
            "one name over NAME_MAX": self.root / ("n" * 300) / tail,
            "missing long names": self.root / Path(*(["q" * 200] * 30)) / tail,
            "existing tree past PATH_MAX then tail": self.over / Path(*(["p" * 200] * 25)) / tail,
            "symlink target holding missing/..": self.root / "L" / "sav" / tail / "s.ess",
            "dot-parent after a symlink (native path)":
                str(self.root / "savelink") + "/" + "x/../" * 1000 + "s.ess",
            "relative": os.path.relpath(self.root / "savelink", os.getcwd()) + "/" + "r/" * 600,
        }
        for name, value in shapes.items():
            with self.subTest(name):
                self.same_as_native(value)

    def test_a_symlink_into_a_protected_root_is_still_followed(self):
        # The review's attack shape: a symlinked prefix and a long tail must still land in the root.
        for value in (self.root / "savelink" / Path(*(["m"] * 1200)) / "s.ess",
                      str(self.root / "savelink") + "/" + "x/../" * 1000 + "s.ess",
                      self.root / "L" / "sav" / Path(*(["m"] * 1200)) / "s.ess"):
            with self.subTest(str(value)[-40:]):
                self.assertTrue(preservation.contains(self.saves, value))

    def test_a_long_missing_tail_needs_few_probes_and_one_boundary_resolution(self):
        # One shape per error class the search treats as missing: ENOENT, ENOTDIR, ENAMETOOLONG.
        shapes = {
            "absent": self.root / "MISSING" / Path(*(["a"] * 10_000)) / "last",
            "under a file": self.root / "afile" / Path(*(["a"] * 10_000)),
            "name over NAME_MAX": self.root / ("n" * 300) / Path(*(["a"] * 10_000)),
        }
        real = Path.resolve
        for name, target in shapes.items():
            calls = []

            def resolve(value, strict=False):
                calls.append(strict)
                return real(value, strict=strict)

            with self.subTest(name), patch.object(Path, "resolve", resolve):
                result = profiles.canonical(target)
            self.assertEqual(os.path.normcase(str(target)), result, name)
            self.assertLessEqual(len(calls), 20, name)
            self.assertEqual(1, sum(not strict for strict in calls), name)

    def test_cost_grows_linearly_with_a_missing_tail(self):
        # Quadratic is ~16x for 4x the length, linear ~4x (plus a log factor from the probes).
        # The bound is loose so a busy runner cannot fail it.
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

        small, large = cost(20_000), cost(80_000)
        self.assertLess(large / small, 10, "4x the length cost {:.1f}x".format(large / small))


if __name__ == "__main__":
    unittest.main()
