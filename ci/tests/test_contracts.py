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

    def test_shared_verifier_accepts_a_pinned_component_with_version_comment(self):
        block = '\n      - uses: WingedGuardian/modding-agent-harness/.github/actions/ci-policy@' + 'a'*40 + ' # v1 policy API\n'
        block += '        with:\n          mode: verify\n          required-jobs: \'["fast"]\'\n          needs-json: ${{ toJSON(needs) }}\n'
        contracts.verify_action(block, {"fast"})

    def test_core_fixtures_use_the_same_volume_canonical_runner_temp(self):
        full = (ROOT / ".github/workflows/full-qualification.yml").read_text()
        self.assertIn('          TEMP: ${{ runner.temp }}', full)
        self.assertIn('          TMP: ${{ runner.temp }}', full)
        self.assertNotIn('\n      TEMP: ${{ runner.temp }}', full)

    def test_x4_historical_comments_do_not_demote_a_live_platform(self):
        matrix = ('      # Previously experimental: true, later experimental: false\n'
                  '        - os: windows-latest\n          experimental: false\n'
                  '        - os: ubuntu-latest\n          experimental: false\n')
        contracts.x4_matrix(matrix)
        with self.assertRaises(contracts.ContractError):
            contracts.x4_matrix(matrix.replace('          experimental: false', '          experimental: true', 1))

    def test_job_comments_are_not_jobs_but_unknown_job_layout_refuses(self):
        text = "name: CI\njobs:\n  # A documented boundary\n  fast:\n    runs-on: ubuntu-latest\n"
        self.assertEqual(set(contracts.jobs(text)), {"fast"})
        with self.assertRaises(contracts.ContractError):
            contracts.jobs("name: CI\njobs:\n  - hidden-job\n")

    SKYRIM_SHARDS = ('      matrix:\n        os: [windows-latest, ubuntu-latest]\n        part: [suite]\n        include:\n'
                     '          - {os: windows-latest, python: "3.10", part: mutations, shard: 1, shards: 2}\n'
                     '          - {os: windows-latest, python: "3.10", part: mutations, shard: 2, shards: 2}\n'
                     '          - {os: ubuntu-latest, python: "3.10", part: mutations, shard: 1, shards: 1}\n')
    SKYRIM_TOTALS = ('      matrix:\n        include:\n          - {os: windows-latest, leg: Windows, shards: 2}\n'
                     '          - {os: ubuntu-latest, leg: Linux, shards: 1}\n'
                     '        run: python scripts/check_ci_leg.py log "${logs[@]}" --leg "$LEG" --part mutations\n')

    def test_skyrim_mutation_shards_must_be_complete_and_summed(self):
        contracts.skyrim_shards(self.SKYRIM_SHARDS, self.SKYRIM_TOTALS)
        bad = [(self.SKYRIM_SHARDS.replace("shard: 2, shards: 2}\n", ""), self.SKYRIM_TOTALS),       # a slice nobody runs
               (self.SKYRIM_SHARDS.replace("shard: 2, shards: 2}", "shard: 2, shards: 3}"), self.SKYRIM_TOTALS),
               (self.SKYRIM_SHARDS.replace('python: "3.10", part: mutations, shard: 1, shards: 1',
                                           'python: "3.13", part: mutations, shard: 1, shards: 1'), self.SKYRIM_TOTALS),
               (self.SKYRIM_SHARDS.replace("          - {os: ubuntu-latest, python: \"3.10\", part: mutations, shard: 1, shards: 1}\n", ""),
                self.SKYRIM_TOTALS),                                                                   # one OS only
               (self.SKYRIM_SHARDS, self.SKYRIM_TOTALS.replace("leg: Windows, shards: 2", "leg: Windows, shards: 1")),
               (self.SKYRIM_SHARDS, self.SKYRIM_TOTALS.replace("--part mutations", "")),
               (self.SKYRIM_SHARDS.replace("part: mutations", "part: mutation"), self.SKYRIM_TOTALS)]
        for matrix, totals in bad:
            with self.subTest(matrix=matrix[-120:], totals=totals[-80:]), self.assertRaises(contracts.ContractError):
                contracts.skyrim_shards(matrix, totals)

    def test_the_parallel_cap_is_the_account_concurrency(self):
        self.assertEqual(contracts.MAX_PARALLEL, 20)
        self.assertIn("mutations-total", contracts.FULL["skyrim"])

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
                 ("full", "max-parallel: 8", "max-parallel: 21"),
                 ("full", "max-parallel: 8", "max-parallel: 0"),
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
