"""Release check and fetch: nothing here may touch the real network, and nothing installed may change.

Every GitHub call goes through an injected fake; `urlopen` itself is replaced so a test that forgot the fake fails
instead of reaching GitHub.
"""
import hashlib
import io
import json
import stat
import tempfile
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

from modding_harness import installation, release

REPO = "owner/toolkit"
COMMIT = "a" * 40


def profile(version="4.2.0"):
    return release.ReleaseProfile(REPO, "toolkit-{version}.zip", "the toolkit",
                                  lambda root: version)


def make_zip(version="4.3.0", commit=COMMIT, extra=None, root_name=None, info_version=None):
    buf = io.BytesIO()
    top = root_name or "toolkit-" + version
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(top + "/BUILD-INFO", "commit={}\nversion={}\nbuilt-from=git archive\n".format(commit, info_version or version))
        z.writestr(top + "/setup.sh", "echo setup\n")
        z.writestr(top + "/tools/a.py", "print(1)\n")
        for name, data in (extra or {}).items():
            if isinstance(data, zipfile.ZipInfo):
                z.writestr(data, b"x")
            else:
                z.writestr(name, data)
    return buf.getvalue()


class FakeGitHub:
    def __init__(self, version="4.3.0", body=None, digest=None, draft=False, prerelease=False, commit=COMMIT,
                 asset_name=None, url=None, fail=None):
        self.body = make_zip(version) if body is None else body
        self.version, self.commit, self.fail = version, commit, fail
        self.digest = "sha256:" + hashlib.sha256(self.body).hexdigest() if digest is None else digest
        self.name = asset_name or "toolkit-{}.zip".format(version)
        self.url = url or "https://github.com/{}/releases/download/v{}/{}".format(REPO, version, self.name)
        self.draft, self.prerelease, self.calls = draft, prerelease, []

    def __call__(self, url, limit=release.MAX_META, accept=None, stream_to=None):
        self.calls.append(url)
        if self.fail:
            raise self.fail
        if url.endswith("/releases/latest"):
            return json.dumps({"tag_name": "v" + self.version, "draft": self.draft, "prerelease": self.prerelease,
                               "assets": [{"name": self.name, "size": len(self.body), "digest": self.digest,
                                           "browser_download_url": self.url}]}).encode()
        if "/commits/" in url:
            return json.dumps({"sha": self.commit}).encode()
        if url == self.url:
            stream_to.write(self.body)
            return hashlib.sha256(self.body).hexdigest(), len(self.body)
        raise AssertionError("unexpected URL " + url)


def _no_network(*args, **kwargs):
    raise AssertionError("a test reached for the real network")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "install"
        self.root.mkdir()
        (self.root / "setup.sh").write_text("old\n")
        patcher = mock.patch.object(urllib.request, "urlopen", _no_network)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = {}

    def installed_bytes(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*")
                if p.is_file() and ".modding-agent-harness" not in p.parts}


class VersionTests(unittest.TestCase):
    def test_versions_compare_numerically_not_as_text(self):
        self.assertTrue(release.newer("4.10.0", "4.9.0"))
        self.assertTrue(release.newer("v4.3", "4.2.0"))
        self.assertFalse(release.newer("4.2.0", "4.2"))
        self.assertFalse(release.newer("4.1.9", "4.2.0"))

    def test_an_unreadable_version_is_never_newer(self):
        self.assertFalse(release.newer("4.3.0-rc1", "4.2.0"))
        self.assertFalse(release.newer("4.3.0", None))
        self.assertFalse(release.newer(None, "4.2.0"))


class CheckTests(Base):
    def test_a_newer_release_is_found_cached_and_named_once(self):
        gh = FakeGitHub()
        state = release.check(self.root, profile(), gh, now=1000, environ=self.env)
        self.assertEqual((state["status"], state["latest"]), ("checked", "4.3.0"))
        line = release.note(self.root, profile(), environ=self.env)
        self.assertIn("4.3.0 is out (you have 4.2.0)", line)
        self.assertEqual(release.note(self.root, profile(), environ=self.env), "")   # once per version

    def test_the_note_reads_only_the_cache(self):
        release.check(self.root, profile(), FakeGitHub(), now=1000, environ=self.env)
        with mock.patch.object(release, "http_get", side_effect=AssertionError("network")):
            self.assertTrue(release.note(self.root, profile(), environ=self.env))

    def test_github_is_asked_at_most_once_a_day_unless_forced(self):
        gh = FakeGitHub()
        release.check(self.root, profile(), gh, now=1000, environ=self.env)
        release.check(self.root, profile(), gh, now=1000 + release.INTERVAL - 1, environ=self.env)
        self.assertEqual(len(gh.calls), 1)
        release.check(self.root, profile(), gh, now=1000 + release.INTERVAL, environ=self.env)
        self.assertEqual(len(gh.calls), 2)
        release.check(self.root, profile(), gh, now=1000 + release.INTERVAL, force=True, environ=self.env)
        self.assertEqual(len(gh.calls), 3)

    def test_the_same_or_an_older_release_says_nothing(self):
        for version in ("4.2.0", "4.1.0"):
            with self.subTest(version=version):
                release.check(self.root, profile(), FakeGitHub(version), force=True, environ=self.env)
                self.assertEqual(release.note(self.root, profile(), environ=self.env), "")

    def test_drafts_and_prereleases_are_not_offered(self):
        for gh in (FakeGitHub(draft=True), FakeGitHub(prerelease=True)):
            release.check(self.root, profile(), gh, force=True, environ=self.env)
            self.assertEqual(release.note(self.root, profile(), environ=self.env), "")

    def test_offline_and_bad_answers_are_silent_and_never_raise(self):
        for gh in (FakeGitHub(fail=OSError("offline")), FakeGitHub(fail=ValueError("bad json")),
                   lambda *a, **k: b"not json", lambda *a, **k: b"[]"):
            state = release.check(self.root, profile(), gh, force=True, environ=self.env)
            self.assertEqual(state["status"], "unreachable")
            self.assertEqual(release.note(self.root, profile(), environ=self.env), "")

    def test_turned_off_by_setting_or_environment_asks_nothing(self):
        gh = FakeGitHub()
        self.assertEqual(release.check(self.root, profile(), gh, force=True,
                                       environ={"MODDING_HARNESS_UPDATE_CHECK": "off"})["status"], "off")
        settings = self.root / release.SETTINGS
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text('{"v": 1, "check": false}')
        self.assertEqual(release.check(self.root, profile(), gh, force=True, environ=self.env)["status"], "off")
        self.assertEqual(gh.calls, [])

    def test_the_setting_is_a_protected_local_file(self):
        self.assertIn(release.SETTINGS, installation.LOCAL_ONLY)


class FetchTests(Base):
    def test_a_verified_release_is_unpacked_aside_and_nothing_installed_changes(self):
        before = self.installed_bytes()
        out = release.fetch(self.root, profile(), FakeGitHub())
        self.assertEqual(self.installed_bytes(), before)
        self.assertFalse(out["installed_changed"])
        candidate = json.loads(Path(out["candidate"]).read_text())
        self.assertEqual(set(candidate["files"]), {"BUILD-INFO", "setup.sh", "tools/a.py"})
        self.assertEqual(candidate["files"]["setup.sh"], hashlib.sha256(b"echo setup\n").hexdigest())
        self.assertTrue((Path(out["source"]) / "tools/a.py").is_file())

    def test_a_digest_mismatch_refuses_and_keeps_no_zip(self):
        gh = FakeGitHub(digest="sha256:" + "0" * 64)
        with self.assertRaisesRegex(ValueError, "SHA-256 does not match"):
            release.fetch(self.root, profile(), gh)
        self.assertEqual(list((self.root / release.UPDATES).rglob("*.zip")), [])

    def test_no_published_digest_refuses(self):
        with self.assertRaisesRegex(ValueError, "no SHA-256 digest"):
            release.fetch(self.root, profile(), FakeGitHub(digest=""))

    def test_a_download_address_outside_the_repository_refuses(self):
        with self.assertRaisesRegex(ValueError, "not this repository"):
            release.fetch(self.root, profile(), FakeGitHub(url="https://example.invalid/toolkit-4.3.0.zip"))

    def test_nothing_newer_refuses(self):
        with self.assertRaisesRegex(ValueError, "no newer release"):
            release.fetch(self.root, profile("4.3.0"), FakeGitHub())

    def test_build_info_must_name_the_release_and_its_commit(self):
        cases = {"version": make_zip(info_version="4.2.9"),
                 "commit": make_zip(commit="b" * 40)}
        for label, body in cases.items():
            with self.subTest(label):
                with self.assertRaisesRegex(ValueError, "BUILD-INFO"):
                    release.fetch(self.root, profile(), FakeGitHub(body=body))

    def test_a_damaged_zip_refuses_cleanly(self):
        body = bytearray(make_zip())
        body[40] ^= 0xFF                                  # inside the first member's data: its CRC no longer matches
        with self.assertRaisesRegex(ValueError, "damaged"):
            release.fetch(self.root, profile(), FakeGitHub(body=bytes(body)))

    def test_unsafe_zips_refuse(self):
        link = zipfile.ZipInfo("toolkit-4.3.0/link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases = {"dotdot": {"toolkit-4.3.0/../evil.py": "x"}, "absolute": {"/evil.py": "x"},
                 "drive": {"C:/evil.py": "x"}, "two roots": {"other/evil.py": "x"},
                 "case twins": {"toolkit-4.3.0/Setup.sh": "x"}, "link": {"toolkit-4.3.0/link": link}}
        for label, extra in cases.items():
            with self.subTest(label):
                before = self.installed_bytes()
                with self.assertRaises(ValueError):
                    release.fetch(self.root, profile(), FakeGitHub(body=make_zip(extra=extra)))
                self.assertEqual(self.installed_bytes(), before)
                self.assertFalse((self.root / "evil.py").exists())
                self.assertFalse((self.root.parent / "evil.py").exists())


if __name__ == "__main__":
    unittest.main()
