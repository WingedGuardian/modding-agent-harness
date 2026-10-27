"""Explicit offline maintainer update from immutable Git objects, never dirty files."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def git(source, *args):
    return subprocess.check_output(["git", "-C", str(source), *args])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    target = args.target.resolve()
    lockfile = target / "tools/modding-harness.lock.json"
    if args.check:
        lock = json.loads(lockfile.read_text())
        actual = {p.relative_to(target).as_posix() for p in (target / "tools/modding_harness").rglob("*") if p.is_file()}
        if actual != set(lock["files"]):
            raise SystemExit("vendored file inventory differs from the pinned manifest")
        for rel, expected in lock["files"].items():
            if hashlib.sha256((target / rel).read_bytes()).hexdigest() != expected:
                raise SystemExit("vendored byte mismatch: " + rel)
        print("verified {} vendored files at {}".format(len(lock["files"]), lock["revision"]))
        return
    revision = git(source, "rev-parse", args.revision + "^{commit}").decode().strip()
    names = git(source, "ls-tree", "-r", "--name-only", revision, "modding_harness").decode().splitlines()
    files = {"tools/" + name: git(source, "show", revision + ":" + name) for name in names
             if name.endswith((".py", ".mjs"))}
    files["tools/modding_harness/LICENSE"] = git(source, "show", revision + ":LICENSE")
    # Refuse an unexpected existing package rather than silently deleting files.
    package = target / "tools/modding_harness"
    if package.exists():
        old = json.loads(lockfile.read_text()) if lockfile.exists() else {"files": {}}
        for file in package.rglob("*"):
            if file.is_file() and file.relative_to(target).as_posix() not in old["files"]:
                raise SystemExit("unowned vendored file: " + str(file))
        for rel, expected in old["files"].items():
            file = target / rel
            if file.exists() and hashlib.sha256(file.read_bytes()).hexdigest() != expected:
                raise SystemExit("locally modified vendored file: " + rel)
        if set(old["files"]) - set(files):
            raise SystemExit("this update removes files; review the retired owned paths before updating")
    for rel, body in files.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    lock = {"v": 1, "project": "modding-agent-harness", "revision": revision,
            "api_version": 1, "files": {rel: hashlib.sha256(body).hexdigest() for rel, body in sorted(files.items())}}
    lockfile.write_text(json.dumps(lock, indent=2) + "\n", encoding="ascii", newline="\n")
    print("vendored {} files from {}".format(len(files), revision))


if __name__ == "__main__":
    main()
