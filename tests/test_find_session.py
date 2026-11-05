import io
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
import unittest
from unittest import mock

from modding_harness import find_session

NODE = shutil.which("node")

# A stand-in for episodic-memory's dist/search.js: returns canned hits, records what it was asked.
FAKE_SEARCH = """
import fs from 'node:fs';
export async function searchConversations(q, opts) {
  if (process.env.FAKE_NOISE) console.log('Migrating schema: added column');
  fs.writeFileSync(process.env.FAKE_LOG, JSON.stringify({ q, opts, cfg: process.env.EPISODIC_MEMORY_CONFIG_DIR,
    nosync: process.env.EPISODIC_MEMORY_DISABLE_AUTO_SYNC, nosum: process.env.EPISODIC_MEMORY_SKIP_SUMMARIES }));
  return JSON.parse(fs.readFileSync(process.env.FAKE_HITS, 'utf8'));
}
"""
FAKE_CLI = """
import fs from 'node:fs';
fs.writeFileSync(process.env.FAKE_SYNC_LOG, JSON.stringify({ argv: process.argv.slice(2),
  nosum: process.env.EPISODIC_MEMORY_SKIP_SUMMARIES, cfg: process.env.EPISODIC_MEMORY_CONFIG_DIR }));
if (process.env.FAKE_SYNC_FAIL) process.exit(3);
if (process.env.FAKE_SYNC_SKIP) { console.error('sync already running; skipping'); process.exit(0); }
if (process.env.FAKE_SYNC_SLEEP) await new Promise(r => setTimeout(r, Number(process.env.FAKE_SYNC_SLEEP)));
console.log('Sync complete!\\n  Copied: 2\\n  Indexed: 2');
"""


def hit(session, sim, user, archive="", ts="2026-10-20T10:00:00Z", harness="claude", reply="", lines=(1, 5), cwd=""):
    return {"exchange": {"sessionId": session, "project": "proj", "timestamp": ts, "archivePath": archive, "cwd": cwd,
                         "lineStart": lines[0], "lineEnd": lines[1], "harness": harness, "userMessage": user,
                         "assistantMessage": reply},
            "similarity": sim, "snippet": user}


def flat(h):
    """The shape the node shim prints."""
    return {"similarity": h["similarity"], "snippet": h["snippet"], "e": h["exchange"]}


class GroupingTests(unittest.TestCase):
    def test_hits_fold_per_session_best_first_and_boilerplate_never_counts(self):
        hits = [flat(hit("a", 0.70, "We tuned the missile damage values")),
                flat(hit("b", 0.80, "Bankai lags under heavy load in Whiterun")),
                flat(hit("a", 0.75, "And the torpedo reload times too")),
                flat(hit("c", 0.99, "<system-reminder>injected context</system-reminder>")),
                flat(hit("c", 0.98, "# AGENTS.md instructions for /x")),
                flat(hit("d", 0.97, "<environment_context><cwd>/x</cwd>"))]
        groups = find_session.group(hits)
        self.assertEqual([g["session_id"] for g in groups], ["b", "a"])
        a = groups[1]
        self.assertEqual((a["hits"], a["best"]), (2, 0.75))
        self.assertEqual([p["similarity"] for p in a["passages"]], [0.75, 0.70])
        self.assertEqual(a["resume"], "claude --resume a")

    def test_a_terse_turn_shows_the_reply_that_answered_it(self):
        groups = find_session.group([flat(hit("a", 0.6, "ok do it", reply="Raised the missile damage by 20 percent"))])
        self.assertIn("ok do it -> reply: Raised the missile damage", groups[0]["passages"][0]["snippet"])

    def test_an_injected_turn_is_shown_by_its_reply_and_dropped_without_one(self):
        hits = [flat(hit("a", 0.8, "<task-notification>done</task-notification>",
                         reply="The missile damage is now 20 percent higher")),
                flat(hit("b", 0.9, "Base directory for this skill: /x")),
                flat(hit("c", 0.9, "<local-command-caveat>Caveat</local-command-caveat>"))]
        groups = find_session.group(hits)
        self.assertEqual([g["session_id"] for g in groups], ["a"])
        self.assertEqual(groups[0]["passages"][0]["snippet"], "reply: The missile damage is now 20 percent higher")

    def test_resume_names_the_directory_it_must_run_from(self):
        groups = find_session.group([flat(hit("a", 0.6, "Port the mod to VR", cwd="C:/Games/Skyrim"))])
        self.assertIn("resume: claude --resume a  (run from C:/Games/Skyrim)", find_session.render("q", groups))

    def test_codex_sessions_resume_with_codex(self):
        groups = find_session.group([flat(hit("x", 0.6, "Port the mod to VR", harness="codex"))])
        self.assertEqual(groups[0]["resume"], "codex resume x")

    def test_malformed_hits_are_skipped(self):
        self.assertEqual(find_session.group([None, {}, {"e": "x"}, {"e": {"userMessage": "no id"}}]), [])

    def test_first_prompt_skips_injected_turns_in_both_transcript_formats(self):
        with tempfile.TemporaryDirectory() as d:
            cc = Path(d) / "cc.jsonl"
            cc.write_text("\n".join(json.dumps(o) for o in [
                {"type": "user", "message": {"content": "<command-name>/clear</command-name>"}},
                {"type": "user", "isMeta": True, "message": {"content": "Caveat: meta"}},
                {"type": "user", "message": {"content": [{"type": "text", "text": "Port the mod to VR"}]}}]),
                encoding="utf-8")
            cx = Path(d) / "cx.jsonl"
            cx.write_text("\n".join(json.dumps(o) for o in [
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "# AGENTS.md instructions for C:/x"}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "Tune the missiles"}]}}]), encoding="utf-8")
            self.assertEqual(find_session.first_prompt(cc), "Port the mod to VR")
            self.assertEqual(find_session.first_prompt(cx), "Tune the missiles")
            self.assertEqual(find_session.first_prompt(Path(d) / "missing.jsonl"), "")

    def test_several_matches_tell_the_agent_to_ask(self):
        groups = find_session.group([flat(hit("a", 0.7, "missiles one")), flat(hit("b", 0.6, "missiles two"))])
        text = find_session.render("missiles", groups, "index synced (0 new or changed session(s) indexed)")
        self.assertTrue(text.startswith("index synced"))
        self.assertIn("Do not pick one", text)
        one = find_session.render("missiles", groups[:1])
        self.assertNotIn("Do not pick one", one)


class IndexLocationTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.env = mock.patch.dict(os.environ, {"USERPROFILE": self.home, "HOME": self.home}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        for k in ("EPISODIC_MEMORY_CONFIG_DIR", "XDG_CONFIG_HOME"):
            os.environ.pop(k, None)

    def index_at(self, d):
        p = Path(d) / "conversation-index"
        p.mkdir(parents=True)
        (p / "db.sqlite").write_bytes(b"")
        return Path(d)

    def test_no_index_anywhere_is_an_error_naming_where_it_looked(self):
        with self.assertRaises(find_session.SearchUnavailable) as cm:
            find_session.config_dir()
        self.assertIn("episodic-memory-data", str(cm.exception))
        self.assertIn("silently return nothing", str(cm.exception))

    def test_the_documented_data_dir_wins_over_the_empty_tool_default(self):
        want = self.index_at(Path(self.home) / ".local" / "share" / "episodic-memory-data")
        (Path(self.home) / ".config" / "superpowers").mkdir(parents=True)    # the empty default
        self.assertEqual(find_session.config_dir(), want)

    def test_an_explicit_variable_without_an_index_does_not_fall_through(self):
        self.index_at(Path(self.home) / ".local" / "share" / "episodic-memory-data")
        os.environ["EPISODIC_MEMORY_CONFIG_DIR"] = str(Path(self.home) / "elsewhere")
        with self.assertRaises(find_session.SearchUnavailable) as cm:
            find_session.config_dir()
        self.assertIn("holds no index", str(cm.exception))


@unittest.skipIf(NODE is None, "node is not installed")
class EndToEndTests(unittest.TestCase):
    """The real node, a fake episodic-memory: what the shim passes and what main prints."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.home), True)
        em = self.home / ".local" / "share" / "episodic-memory"
        (em / "dist").mkdir(parents=True)
        (em / "cli").mkdir()
        (em / "dist" / "search.js").write_text(FAKE_SEARCH, encoding="utf-8")
        (em / "cli" / "episodic-memory.js").write_text(FAKE_CLI, encoding="utf-8")
        (em / "package.json").write_text('{"type": "module"}', encoding="utf-8")
        idx = self.home / ".local" / "share" / "episodic-memory-data" / "conversation-index"
        idx.mkdir(parents=True)
        (idx / "db.sqlite").write_bytes(b"")
        self.hits = self.home / "hits.json"
        self.log = self.home / "log.json"
        self.sync_log = self.home / "sync.json"
        env = {"USERPROFILE": str(self.home), "HOME": str(self.home), "FAKE_HITS": str(self.hits),
               "FAKE_LOG": str(self.log), "FAKE_SYNC_LOG": str(self.sync_log)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in ("EPISODIC_MEMORY_CONFIG_DIR", "FIND_SESSION_EPISODIC_DIR", "FAKE_SYNC_FAIL", "FAKE_SYNC_SKIP",
                  "FAKE_SYNC_SLEEP", "FAKE_NOISE", "PERSONAL_SUPERPOWERS_DIR",
                  "EPISODIC_MEMORY_SKIP_SUMMARIES", "EPISODIC_MEMORY_DISABLE_AUTO_SYNC"):
            os.environ.pop(k, None)

    def run_main(self, *args):
        out, err = io.BytesIO(), io.StringIO()
        stdout = mock.Mock()
        stdout.buffer = out
        with mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stderr", err):
            code = find_session.main(list(args))
        return code, out.getvalue().decode("utf-8"), err.getvalue()

    def test_it_syncs_then_searches_semantically_in_the_resolved_index(self):
        self.hits.write_text(json.dumps([hit("a", 0.7, "We tuned the missile damage values"),
                                         hit("b", 0.6, "Missile reload times")]), encoding="utf-8")
        code, out, _ = self.run_main("missile balance tweaks")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("index synced (2 new or changed session(s) indexed)"), out)
        self.assertIn("2 session(s) match", out)
        asked = json.loads(self.log.read_text(encoding="utf-8"))
        cfg = str(self.home / ".local" / "share" / "episodic-memory-data")
        self.assertEqual(asked["q"], "missile balance tweaks")
        self.assertEqual((asked["opts"]["mode"], asked["opts"]["include_sidechains"]), ("vector", False))
        self.assertEqual((asked["cfg"], asked["nosync"], asked["nosum"]), (cfg, "1", "1"))
        synced = json.loads(self.sync_log.read_text(encoding="utf-8"))
        self.assertEqual(synced["argv"], ["sync", "--summary-limit", "0"])
        self.assertEqual((synced["cfg"], synced["nosum"]), (cfg, "1"))

    def test_a_failed_sync_still_searches_and_says_the_index_may_be_stale(self):
        os.environ["FAKE_SYNC_FAIL"] = "1"
        self.hits.write_text(json.dumps([hit("a", 0.7, "We tuned the missile damage values")]), encoding="utf-8")
        code, out, _ = self.run_main("missiles")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("index sync failed (exit 3: "), out)
        self.assertIn("results may miss recent sessions", out.splitlines()[0])

    def test_no_sync_skips_the_update_and_says_so(self):
        self.hits.write_text("[]", encoding="utf-8")
        code, out, _ = self.run_main("--no-sync", "missiles")
        self.assertEqual(code, 1)
        self.assertFalse(self.sync_log.exists())
        self.assertIn("index not synced (--no-sync)", out)
        self.assertIn("No session matched", out)

    def test_json_carries_the_index_status(self):
        self.hits.write_text(json.dumps([hit("a", 0.7, "We tuned the missile damage values")]), encoding="utf-8")
        code, out, _ = self.run_main("--json", "missiles")
        doc = json.loads(out)
        self.assertEqual((code, doc["sessions"][0]["session_id"]), (0, "a"))
        self.assertIn("index synced", doc["index"])

    def test_a_sync_skipped_by_another_run_is_not_reported_as_a_sync(self):
        os.environ["FAKE_SYNC_SKIP"] = "1"
        self.hits.write_text(json.dumps([hit("a", 0.7, "We tuned the missile damage values")]), encoding="utf-8")
        _, out, _ = self.run_main("missiles")
        self.assertTrue(out.startswith("index sync skipped (sync already running; skipping)"), out)

    def test_a_slow_sync_is_left_running_and_the_search_still_answers(self):
        os.environ["FAKE_SYNC_SLEEP"] = "4000"
        self.hits.write_text(json.dumps([hit("a", 0.7, "We tuned the missile damage values")]), encoding="utf-8")
        with mock.patch.object(find_session, "SYNC_WAIT", 0.5):
            code, out, _ = self.run_main("missiles")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("index sync still running after 0.5 s (not stopped"), out)
        self.assertIn("1 session(s) match", out)
        logs = list((self.home / ".local" / "share" / "episodic-memory-data").glob("find-session-sync-*.log"))
        self.assertEqual(len(logs), 1)          # kept for the sync still writing it
        deadline = time.time() + 20
        while not self.sync_log.exists() and time.time() < deadline:
            time.sleep(0.2)
        self.assertTrue(self.sync_log.exists(), "the sync left running did not finish on its own")

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_the_sync_runs_in_its_own_process_group(self):
        seen = {}
        real = find_session.subprocess.Popen

        def spy(*a, **kw):
            seen.update(kw)
            return real(*a, **kw)
        self.hits.write_text("[]", encoding="utf-8")
        with mock.patch.object(find_session.subprocess, "Popen", spy):
            self.run_main("missiles")
        self.assertTrue(seen.get("start_new_session"))

    def test_notices_printed_before_the_results_do_not_break_parsing(self):
        os.environ["FAKE_NOISE"] = "1"
        self.hits.write_text(json.dumps([hit("a", 0.7, "We tuned the missile damage values")]), encoding="utf-8")
        code, out, _ = self.run_main("--no-sync", "missiles")
        self.assertEqual(code, 0, out)

    def test_a_batch_shim_for_node_is_refused(self):
        with mock.patch.object(find_session.shutil, "which", return_value="C:/nodejs/node.cmd"):
            code, _, err = self.run_main("missiles")
        self.assertEqual(code, 2)
        self.assertIn("batch shim", err)

    def test_not_installed_is_exit_2_with_the_reason(self):
        shutil.rmtree(str(self.home / ".local" / "share" / "episodic-memory"))
        code, out, err = self.run_main("missiles")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("FIND SESSION: NOT RUN -- episodic-memory is not installed", err)


if __name__ == "__main__":
    unittest.main()
