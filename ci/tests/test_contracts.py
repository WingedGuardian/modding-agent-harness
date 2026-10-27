"""The workflow contract must reject planted coverage and gating failures."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ci_contracts", ROOT / "ci/contracts.py")
contracts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contracts)


class ContractTests(unittest.TestCase):
    def test_current_core_workflow_has_every_required_boundary(self):
        self.assertEqual(contracts.check_contract(ROOT, "core")["full_jobs"], ["stdlib"])

    def test_job_comments_are_not_jobs_but_unknown_job_layout_refuses(self):
        text = "name: CI\njobs:\n  # A documented boundary\n  fast:\n    runs-on: ubuntu-latest\n"
        self.assertEqual(set(contracts.jobs(text)), {"fast"})
        with self.assertRaises(contracts.ContractError):
            contracts.jobs("name: CI\njobs:\n  - hidden-job\n")

    def test_each_planted_weakened_contract_refuses(self):
        main = (ROOT / ".github/workflows/ci.yml").read_text()
        full = (ROOT / ".github/workflows/full-qualification.yml").read_text()
        cases = [("main", "ready_for_review", "closed"),
                 ("main", "needs: [policy, fast]", "needs: [policy]"),
                 ("main", contracts.FULL_IF, "${{ true }}"),
                 ("main", "      queue: max", "      queue: single"),
                 ("main", "      cancel-in-progress: false", "      cancel-in-progress: true"),
                 ("main", "mode: verify", "mode: report"),
                 ("main", "      - uses: ./.github/actions/ci-policy\n        with:\n          profile: core\n          mode: verify", "      - uses: other/action@" + "a"*40 + "\n        with:\n          profile: core\n          mode: verify"),
                 ("main", "needs-json: ${{ toJSON(needs) }}", "needs-json: '{}'"),
                 ("main", "timeout-minutes: 10", "timeout-minutes: 0"),
                 ("full", "max-parallel: 4", "max-parallel: 16"),
                 ("full", "'3.10', '3.11', '3.12', '3.13'", "'3.13'"),
                 ("full", "ubuntu-latest, windows-latest", "ubuntu-latest"),
                 ("full", "    timeout-minutes: 10", "    timeout-minutes: 999"),
                 ("full", "    if: ${{ always() }}", "    if: ${{ success() }}")]
        for which, old, new in cases:
            with self.subTest(which=which, old=old), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                directory = root / ".github/workflows"
                directory.mkdir(parents=True)
                self.assertIn(old, main if which == "main" else full)
                (directory/"ci.yml").write_text(main.replace(old,new,1) if which == "main" else main)
                (directory/"full-qualification.yml").write_text(full.replace(old,new,1) if which == "full" else full)
                with self.assertRaises(contracts.ContractError):
                    contracts.check_contract(root, "core")


if __name__ == "__main__":
    unittest.main()
