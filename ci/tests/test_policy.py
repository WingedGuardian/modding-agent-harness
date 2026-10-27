"""Targeted tests of the shared development policy, without games or GitHub calls."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ci_policy", ROOT / "ci/policy.py")
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)
HEAD, BASE, CANDIDATE = "a"*40, "b"*40, "c"*40


def event(draft=False, action="synchronize"):
    return dict(action=action, pull_request=dict(number=7, draft=draft, head=dict(sha=HEAD), base=dict(sha=BASE)))


class ContextTests(unittest.TestCase):
    def test_draft_updates_are_fast_only(self):
        for action in ("opened", "reopened", "synchronize", "converted_to_draft"):
            with self.subTest(action=action):
                row = policy.context("skyrim", "pull_request", event(True, action), CANDIDATE, "owner/repo")
                self.assertIs(row["full_required"], False)
                self.assertEqual(row["qualification"], "draft-fast-checks-only")

    def test_ready_and_subsequent_updates_require_full(self):
        for profile in policy.PROFILES:
            for action in ("opened", "reopened", "ready_for_review", "synchronize"):
                with self.subTest(profile=profile, action=action):
                    row = policy.context(profile, "pull_request", event(False, action), CANDIDATE, "owner/repo")
                    self.assertIs(row["full_required"], True)
                    self.assertEqual((row["head_sha"], row["base_sha"], row["candidate_sha"]), (HEAD, BASE, CANDIDATE))

    def test_push_and_manual_keep_full_qualification(self):
        for name in ("push", "workflow_dispatch"):
            with self.subTest(name=name):
                row = policy.context("core", name, {}, HEAD, "owner/repo")
                self.assertTrue(row["full_required"])
                self.assertEqual(row["head_sha"], HEAD)

    def test_unknown_or_missing_identity_refuses(self):
        cases = [("other", "pull_request", event(), CANDIDATE, "owner/repo"),
                 ("core", "pull_request_target", event(), CANDIDATE, "owner/repo"),
                 ("core", "pull_request", {}, CANDIDATE, "owner/repo"),
                 ("core", "push", {}, "", "owner/repo"),
                 ("core", "push", {}, HEAD, "bad\nname")]
        for args in cases:
            with self.subTest(args=args[:2]), self.assertRaises(policy.PolicyError):
                policy.context(*args)
        for key in ("draft", "number", "head", "base"):
            bad = event()
            del bad["pull_request"][key]
            with self.subTest(key=key), self.assertRaises(policy.PolicyError):
                policy.context("core", "pull_request", bad, CANDIDATE, "owner/repo")
        for value in (None, 0, "false"):
            bad = event(value)
            with self.subTest(value=value), self.assertRaises(policy.PolicyError):
                policy.context("core", "pull_request", bad, CANDIDATE, "owner/repo")

    def test_fresh_commit_identities_are_not_reused(self):
        first = policy.context("x4", "pull_request", event(), CANDIDATE, "owner/repo")
        changed = event()
        changed["pull_request"]["head"]["sha"] = "d"*40
        second = policy.context("x4", "pull_request", changed, "e"*40, "owner/repo")
        self.assertNotEqual(first["head_sha"], second["head_sha"])
        self.assertNotEqual(first["candidate_sha"], second["candidate_sha"])


class VerdictTests(unittest.TestCase):
    def test_every_required_job_must_succeed(self):
        required = ["policy", "fast", "qualification"]
        good = {name: dict(result="success") for name in required}
        self.assertEqual(policy.verify(required, good), {name:"success" for name in required})
        for name in required:
            for result in ("failure", "cancelled", "skipped", "queued", None, True):
                bad = {**good, name:dict(result=result)}
                with self.subTest(name=name, result=result), self.assertRaises(policy.PolicyError):
                    policy.verify(required, bad)

    def test_missing_empty_extra_or_duplicate_inventory_refuses(self):
        cases = [([], {}), (["fast", "fast"], {"fast":dict(result="success")}),
                 (["fast"], {}), (["fast"], {"fast":{}}),
                 (["fast"], {"fast":dict(result="success"), "other":dict(result="success")}),
                 (["bad\njob"], {})]
        for required, results in cases:
            with self.subTest(required=required), self.assertRaises(policy.PolicyError):
                policy.verify(required, results)

    def test_duplicate_json_keys_refuse(self):
        with self.assertRaises(policy.PolicyError):
            policy.read_json('{"fast":{"result":"failure"},"fast":{"result":"success"}}')


class TimingTests(unittest.TestCase):
    def test_queue_is_separate_from_execution_and_missing_times_unknown(self):
        rows = policy.timing_rows([dict(id=1,name="test",status="completed",conclusion="success",run_attempt=2,
            created_at="2026-01-01T00:00:00Z",started_at="2026-01-01T00:10:00Z",completed_at="2026-01-01T00:12:00Z"),
            dict(id=2,name="active",status="queued")])
        self.assertEqual((rows[0]["queued_seconds"],rows[0]["execution_seconds"]), (600,120))
        self.assertIsNone(rows[1]["queued_seconds"])
        self.assertIsNone(rows[1]["execution_seconds"])

    def test_timing_request_uses_only_bound_repository_and_keeps_attempts(self):
        urls = []
        def opener(request, timeout):
            urls.append(request.full_url)
            self.assertEqual(request.get_header("Authorization"), "Bearer fixture-token")
            return io.BytesIO(json.dumps(dict(jobs=[dict(id=1,name="job",status="completed",run_attempt=1)])).encode())
        rows = policy.collect_timings("owner/repo", "123", "fixture-token", opener)
        self.assertEqual(rows[0]["run_attempt"], 1)
        self.assertEqual(urls, ["https://api.github.com/repos/owner/repo/actions/runs/123/jobs?filter=all&per_page=100&page=1"])
        with self.assertRaises(policy.PolicyError):
            policy.collect_timings("https://other", "123", "fixture-token", opener)


class CliTests(unittest.TestCase):
    def env(self, folder, draft=False):
        path = folder / "event.json"
        path.write_text(json.dumps(event(draft)), encoding="utf-8")
        return dict(RUNNER_TEMP=str(folder),GITHUB_EVENT_PATH=str(path),GITHUB_EVENT_NAME="pull_request",
                    GITHUB_SHA=CANDIDATE,GITHUB_REPOSITORY="owner/repo",GITHUB_OUTPUT=str(folder/"outputs"),
                    GITHUB_STEP_SUMMARY=str(folder/"summary"),GITHUB_RUN_ID="123")

    def test_context_outputs_and_records_contain_only_source_metadata(self):
        with tempfile.TemporaryDirectory() as owned:
            folder = Path(owned)
            with patch.dict(os.environ, self.env(folder, True), clear=True):
                self.assertEqual(policy.main(["context", "--profile", "core"]), 0)
            self.assertIn("full_required=false", (folder/"outputs").read_text())
            record = json.loads((folder/"ci-context.json").read_text())
            self.assertEqual(record["head_sha"], HEAD)
            self.assertNotIn("pull_request", record)

    def test_cli_failed_required_job_is_nonzero(self):
        with tempfile.TemporaryDirectory() as owned:
            env = self.env(Path(owned))
            env.update(CI_REQUIRED_JOBS='["qualification"]',CI_NEEDS_JSON='{"qualification":{"result":"skipped"}}')
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(policy.main(["verify", "--profile", "core"]), 1)

    def test_draft_cannot_create_a_successful_merge_gate(self):
        with tempfile.TemporaryDirectory() as owned:
            env = self.env(Path(owned), True)
            env.update(CI_REQUIRED_JOBS='["fast"]',CI_NEEDS_JSON='{"fast":{"result":"success"}}')
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(policy.main(["verify", "--profile", "core"]), 1)

    def test_timing_failure_is_visible_and_cannot_supply_a_qualification_verdict(self):
        with tempfile.TemporaryDirectory() as owned:
            folder = Path(owned)
            with patch.dict(os.environ, self.env(folder), clear=True), patch.object(policy,"collect_timings",side_effect=OSError()):
                self.assertEqual(policy.main(["report", "--profile", "core"]), 0)
            row = json.loads((folder/"ci-report.json").read_text())
            self.assertEqual(row["timing_unavailable"], "OSError")
            self.assertNotEqual(row["qualification"], "successful-required-jobs")

    def test_report_does_not_persist_authentication_or_raw_event_body(self):
        with tempfile.TemporaryDirectory() as owned:
            folder = Path(owned)
            env = self.env(folder)
            env["CI_GITHUB_TOKEN"] = "fixture-auth-must-stay-private"
            raw_event = event()
            raw_event["pull_request"]["body"] = "fixture-PR-body-must-stay-private"
            (folder/"event.json").write_text(json.dumps(raw_event), encoding="utf-8")
            with patch.dict(os.environ, env, clear=True), patch.object(policy, "collect_timings", return_value=[]) as collect:
                self.assertEqual(policy.main(["report", "--profile", "core"]), 0)
                self.assertEqual(collect.call_args.args[2], env["CI_GITHUB_TOKEN"])
            persisted = (folder/"ci-report.json").read_text() + (folder/"summary").read_text()
            self.assertNotIn(env["CI_GITHUB_TOKEN"], persisted)
            self.assertNotIn(raw_event["pull_request"]["body"], persisted)


if __name__ == "__main__":
    unittest.main()
