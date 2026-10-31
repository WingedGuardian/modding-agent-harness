#!/usr/bin/env python3
"""The local pre-push gate: what a push must pass at the desk before it costs a CI round.

    python scripts/prepush-gate.py [--base REV]

Owner ruling 2026-10-31 (every repository): no push without the local gate, installed as the maintainers' git
pre-push hook and never shipped. For this repository the gate is the fast-check job plus the tests for what the push
changes, under a minute:
  1. every tracked Python file parses (the fast job's syntax step);
  2. the shared policy and workflow contract tests (ci/tests), and this repository's own workflows pass the contract;
  3. the conformance tests for the files the push changes (`git diff --name-only BASE...HEAD`): a changed test runs
     itself; a changed module under modding_harness/ runs every tests/test_*<its stem>*.py.

BASE defaults to the branch's upstream, else the merge-base with origin/master. Exit 0 = passed, 1 = a step failed,
2 = could not run.
"""
from __future__ import annotations

import argparse
import ast
import glob
import importlib.util
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _git(*args):
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True).stdout.strip()


def base_rev(explicit):
    if explicit:
        return explicit
    up = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if up and _git("rev-parse", "--verify", "-q", up):
        return up
    for ref in ("origin/master", "origin/main", "master"):
        mb = _git("merge-base", "HEAD", ref)
        if mb:
            return mb
    return ""


def tests_for(changed):
    want = []
    for p in changed:
        if p.startswith("tests/test_") and p.endswith(".py"):
            want.append(p)
        elif p.startswith("modding_harness/") and p.endswith(".py"):
            stem = Path(p).stem
            want.extend(Path(f).relative_to(REPO).as_posix() for f in glob.glob(str(REPO / "tests" / ("test_*" + stem + "*.py"))))
    return [t for i, t in enumerate(want) if t not in want[:i] and (REPO / t).is_file()]


def step(name, fn, failed):
    t = time.time()
    print("== " + name, flush=True)
    try:
        problems = fn()
    except Exception as e:                       # a step that cannot run is a failed step, not a passed one
        problems = ["{}: {}".format(type(e).__name__, e)]
    for p in problems:
        print("   " + p)
    if problems:
        failed.append(name)
    print("   {} in {:.0f}s".format("FAILED" if problems else "ok", time.time() - t), flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="")
    a = ap.parse_args(argv)
    base = base_rev(a.base)
    changed = [l.replace("\\", "/") for l in _git("diff", "--name-only", base + "...HEAD").splitlines() if l.strip()] if base else []
    print("prepush-gate: {} @ {}  base {}  {} changed file(s)".format(
        _git("rev-parse", "--abbrev-ref", "HEAD"), _git("rev-parse", "--short", "HEAD"), base or "(none)", len(changed)))
    failed = []

    def syntax():
        out = []
        for p in subprocess.check_output(["git", "ls-files", "-z"], cwd=REPO).decode().split("\0"):
            if p.endswith(".py"):
                try:
                    ast.parse((REPO / p).read_bytes(), filename=p)
                except SyntaxError as e:
                    out.append("{}: {}".format(p, e))
        return out
    step("tracked Python syntax", syntax, failed)

    def run(cmd, cwd=REPO):
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return [] if r.returncode == 0 else (r.stdout + r.stderr).strip().splitlines()[-30:]
    step("shared policy and workflow contract tests", lambda: run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "ci/tests"]), failed)

    def contract():
        spec = importlib.util.spec_from_file_location("ci_contracts", REPO / "ci" / "contracts.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.check_contract(REPO, "core")
        return []
    step("this repository's workflows pass the contract", contract, failed)

    files = tests_for(changed)
    if files:
        print("   tests for changed files: " + ", ".join(files))
        # tests/ is not a package (CI runs `unittest discover -s tests`), so each file is discovered by its own name
        for f in files:
            step("changed-file conformance tests: " + f,
                 lambda f=f: run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-p", Path(f).name]),
                 failed)
    if failed:
        print("prepush-gate: FAILED ({}) -- fix before pushing; CI would have gone red on it".format(", ".join(failed)))
        return 1
    print("prepush-gate: passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
