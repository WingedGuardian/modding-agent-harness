import json
import tempfile
import unittest
from pathlib import Path

from modding_harness import approval, profiles
from modding_harness.contracts import Action, Result, unknown_tool
from modding_harness.dispatch import Dispatcher, Evaluation, Mutation, SavePlan


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        config = self.root / ".modding-agent-harness/config.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"v": 1, "save_reserve_bytes": 0}))
        self.saves = self.root / "saves"
        self.saves.mkdir()
        self.files = [self.saves / (str(i) + ".save") for i in range(3)]
        for path in self.files:
            path.write_bytes(path.name.encode())
        self.action = Action("claude", "toy-edit", "native", session_id="session", command="edit selected")

    def dispatcher(self, count=1, **options):
        mutations = tuple(Mutation(str(p), "write") for p in self.files[:count])
        return Dispatcher(self.root, profiles.toy([self.saves]),
                          lambda action: Evaluation(Result(evaluation="evaluated"), SavePlan(mutations, **options)))

    def test_one_save_requires_verified_preservation_and_preview_is_distinct(self):
        dispatcher = self.dispatcher()
        self.assertEqual(dispatcher.pre(self.action, preserve=False).result.preservation, "required")
        self.assertFalse((self.root / ".modding-agent-harness/backups/saves").exists())
        result = dispatcher.pre(self.action)
        self.assertEqual(result.result.preservation, "verified")
        self.assertEqual(len(result.transactions), 1)

    def test_bulk_needs_canonical_human_evidence(self):
        dispatcher = self.dispatcher(3)
        self.assertEqual(dispatcher.pre(self.action).result.decision, "deny")
        text = approval.example("session", "write", self.files)
        transcript = self.root / "human.jsonl"
        transcript.write_text(json.dumps({"type": "user", "origin": {"kind": "human"},
                              "message": {"role": "user", "content": text}}))
        self.assertEqual(dispatcher.pre(self.action, str(transcript)).result.preservation, "verified")

    def test_local_restriction_precedes_preservation_and_permission(self):
        dispatcher = self.dispatcher()
        dispatcher.local_restrictions = (lambda action: Result("deny", "evaluated", reason="local runtime restriction"),)
        result = dispatcher.pre(self.action)
        self.assertEqual(result.result.reason, "local runtime restriction")
        self.assertFalse(result.transactions)

    # An ask lets the call run on a human "yes", so it must not bypass the save
    # scope, approval and preservation steps that an allow goes through.
    def asking(self, count=1, local=False, **options):
        mutations = tuple(Mutation(str(p), "write") for p in self.files[:count])
        verdict = Result("ask", "evaluated", reason="unrelated rule asks")
        profile_verdict = Result(evaluation="evaluated") if local else verdict
        dispatcher = Dispatcher(self.root, profiles.toy([self.saves]),
                                lambda action: Evaluation(profile_verdict, SavePlan(mutations, **options)))
        if local:
            dispatcher.local_restrictions = (lambda action: verdict,)
        return dispatcher

    def transactions_on_disk(self):
        store = self.root / ".modding-agent-harness/backups/saves/transactions"
        return sorted(store.glob("*.json")) if store.exists() else []

    def approve_all(self):
        transcript = self.root / "human.jsonl"
        transcript.write_text(json.dumps({"type": "user", "origin": {"kind": "human"}, "message": {
            "role": "user", "content": approval.example("session", "write", self.files)}}))
        return str(transcript)

    def test_ask_with_one_save_is_preserved_before_the_ask(self):
        for local in (False, True):
            with self.subTest(local=local):
                before = len(self.transactions_on_disk())
                outcome = self.asking(local=local).pre(self.action)
                self.assertEqual((outcome.result.decision, outcome.result.preservation), ("ask", "verified"))
                self.assertEqual(outcome.result.reason, "unrelated rule asks")
                self.assertEqual(len(outcome.transactions), 1)
                self.assertEqual(len(self.transactions_on_disk()), before + 1)

    def test_ask_with_bulk_saves_and_no_approval_is_the_approval_deny(self):
        for local in (False, True):
            with self.subTest(local=local):
                outcome = self.asking(3, local=local).pre(self.action)
                self.assertEqual((outcome.result.decision, outcome.result.preservation), ("deny", "required"))
                self.assertIn("typed canonical human approval required", outcome.result.reason)
                self.assertFalse(outcome.transactions)
                self.assertFalse(self.transactions_on_disk())

    def test_ask_with_bulk_saves_and_approval_is_preserved_then_asks(self):
        transcript = self.approve_all()
        for local in (False, True):
            with self.subTest(local=local):
                outcome = self.asking(3, local=local).pre(self.action, transcript)
                self.assertEqual((outcome.result.decision, outcome.result.preservation), ("ask", "verified"))
                self.assertEqual(len(outcome.transactions), 1)

    def test_ask_with_unresolved_save_scope_is_denied(self):
        for local in (False, True):
            with self.subTest(local=local):
                outcome = self.asking(local=local, unresolved="opaque helper").pre(self.action)
                self.assertEqual(outcome.result.decision, "deny")
                self.assertIn("save scope unresolved", outcome.result.reason)
                self.assertFalse(self.transactions_on_disk())

    def test_ask_touching_the_permanent_store_is_denied(self):
        store = self.root / ".modding-agent-harness/backups/saves/x.json"
        action = Action("claude", "toy-edit", "native", session_id="session", command="edit",
                        paths=(str(store),), operations=("write",))
        for local in (False, True):
            with self.subTest(local=local):
                outcome = self.asking(0, local=local).pre(action)
                self.assertEqual(outcome.result.decision, "deny")
                self.assertIn("permanent save store is protected", outcome.result.reason)

    def test_ask_preview_preserves_nothing(self):
        outcome = self.asking().pre(self.action, preserve=False)
        self.assertEqual((outcome.result.decision, outcome.result.preservation), ("ask", "required"))
        self.assertFalse(outcome.transactions)
        self.assertFalse(self.transactions_on_disk())

    def test_ask_without_saves_is_unchanged(self):
        for local in (False, True):
            with self.subTest(local=local):
                outcome = self.asking(0, local=local).pre(self.action)
                self.assertEqual(outcome.result, Result("ask", "evaluated", reason="unrelated rule asks"))
                self.assertFalse(outcome.transactions)

    def test_ask_backup_failure_is_a_deny(self):
        dispatcher = self.asking(known_writer_active=True)
        outcome = dispatcher.pre(self.action)
        self.assertEqual(outcome.result.decision, "deny")
        self.assertFalse(outcome.transactions)

    def test_deny_with_saves_returns_before_any_backup(self):
        mutations = tuple(Mutation(str(p), "write") for p in self.files[:1])
        deny = Result("deny", "evaluated", reason="profile refuses")
        dispatcher = Dispatcher(self.root, profiles.toy([self.saves]),
                                lambda action: Evaluation(deny, SavePlan(mutations)))
        self.assertEqual(dispatcher.pre(self.action).result, deny)
        dispatcher = self.dispatcher()
        dispatcher.local_restrictions = (lambda action: Result("ask", "evaluated", reason="local asks"),
                                         lambda action: Result("deny", "evaluated", reason="local refuses"))
        self.assertEqual(dispatcher.pre(self.action).result.reason, "local refuses")
        dispatcher = self.asking(local=True)
        dispatcher.policy = lambda action: Evaluation(deny, SavePlan(mutations))
        self.assertEqual(dispatcher.pre(self.action).result, deny)
        self.assertFalse(self.transactions_on_disk())

    def test_unknown_warns_allows_and_does_not_claim_evaluation(self):
        dispatcher = Dispatcher(self.root, profiles.toy(), lambda action: Evaluation(unknown_tool(action)))
        result = dispatcher.pre(self.action).result
        self.assertEqual((result.decision, result.evaluation), ("allow", "unguarded"))
        self.assertTrue(result.warnings)
        dispatcher.local_restrictions = (lambda action: Result("deny", "evaluated", reason="installed runtime restriction"),)
        self.assertEqual(dispatcher.pre(self.action).result.decision, "deny")

    def test_incomplete_local_asks_are_refused(self):
        for status in ("failed", "partial"):
            with self.subTest(status=status):
                dispatcher = self.dispatcher()
                dispatcher.local_restrictions = (lambda action: Result("ask", status),)
                outcome = dispatcher.pre(self.action)
                self.assertEqual((outcome.result.decision, outcome.result.evaluation), ("deny", "failed"))
                self.assertFalse(outcome.transactions)

    def test_unknown_with_declared_save_obligations_refuses_even_with_local_ask(self):
        plans = [SavePlan((Mutation(str(self.files[0]), "write"),)),
                 SavePlan(tuple(Mutation(str(p), "write") for p in self.files)),
                 SavePlan(unresolved="opaque helper"), SavePlan(wildcard=True),
                 SavePlan(helper=True), SavePlan(known_writer_active=True), SavePlan(recheck=lambda: ())]
        for plan in plans:
            for local in (False, True):
                with self.subTest(plan=plan, local=local):
                    dispatcher = Dispatcher(self.root, profiles.toy([self.saves]),
                        lambda action: Evaluation(unknown_tool(action), plan),
                        (lambda action: Result("ask", "evaluated"),) if local else ())
                    outcome = dispatcher.pre(self.action)
                    self.assertEqual((outcome.result.decision, outcome.result.evaluation,
                                      outcome.result.preservation), ("deny", "partial", "failed"))
                    self.assertIn("save obligations", outcome.result.reason)
                    self.assertFalse(outcome.transactions)
                    self.assertFalse(self.transactions_on_disk())

    def test_unknown_with_local_ask_keeps_unguarded_coverage_and_warnings(self):
        dispatcher = Dispatcher(self.root, profiles.toy(), lambda action: Evaluation(Result()),
            (lambda action: Result("ask", "evaluated", reason="local asks", warnings=("local warning",)),))
        outcome = dispatcher.pre(self.action)
        self.assertEqual((outcome.result.decision, outcome.result.evaluation), ("ask", "unguarded"))
        self.assertEqual(outcome.result.reason, "local asks")
        self.assertIn("local warning", outcome.result.warnings)
        self.assertTrue(any("WARN AND ALLOW" in warning for warning in outcome.result.warnings))
        self.assertFalse(outcome.transactions)

    def test_unknown_writer_and_missing_membership_recheck_refuse(self):
        for dispatcher in (self.dispatcher(known_writer_active=True), self.dispatcher(helper=True)):
            self.assertEqual(dispatcher.pre(self.action).result.decision, "deny")

    def test_engine_exception_and_incomplete_path_map_are_failed_refusals(self):
        dispatcher = self.dispatcher()
        bad = Action("claude", "edit", "native", paths=(str(self.files[0]),))
        self.assertEqual(dispatcher.pre(bad).result.evaluation, "failed")
        dispatcher.policy = lambda action: (_ for _ in ()).throw(RuntimeError("private server body"))
        result = dispatcher.pre(self.action).result
        self.assertEqual((result.decision, result.evaluation), ("deny", "failed"))
        self.assertNotIn("private server body", result.reason)


if __name__ == "__main__":
    unittest.main()
