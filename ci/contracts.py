"""Fail-closed structural checks for the three repositories' plain workflow layout.

This is not a general YAML parser; unfamiliar layouts require review. GitHub
also validates the complete workflow syntax.
"""
from pathlib import Path
import re

FAST = {"core": {"fast"}, "skyrim": {"shell", "python", "node", "toolkit", "setup-smoke"}, "x4": {"quick", "scrub"}}
FULL = {"core": {"stdlib"}, "skyrim": {"behavior", "dotnet", "resaver-plus", "release-build"}, "x4": {"tests"}}
REQUIRED_IF = "${{ always() && (github.event_name != 'pull_request' || !github.event.pull_request.draft) }}"
FULL_IF = "${{ needs.policy.outputs.full_required == 'true' }}"


class ContractError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ContractError(message)


def jobs(text):
    require(text.count("\njobs:\n") == 1, "expected one plain jobs mapping")
    body = text.split("\njobs:\n", 1)[1]
    headers = list(re.finditer(r"^  (?!#)(\S.*)$", body, re.M))
    require(bool(headers), "empty workflow jobs")
    result = {}
    for index, header in enumerate(headers):
        key = header.group(1)
        require(re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*:\s*(?:#.*)?", key) is not None, "unsupported job layout")
        name = key.split(":")[0]
        require(name not in result, "duplicate job identity")
        result[name] = body[header.end():headers[index+1].start() if index+1 < len(headers) else len(body)]
    return result


def field(block, name):
    matches = re.findall(r"^    " + re.escape(name) + r":\s*(\S.*?)\s*$", block, re.M)
    require(len(matches) == 1, "missing or duplicated job field: " + name)
    return matches[0]


def needed(block):
    value = field(block, "needs")
    require(value.startswith("[") and value.endswith("]"), "needs must use the reviewed inline list")
    names = [name.strip() for name in value[1:-1].split(",") if name.strip()]
    require(len(names) == len(set(names)), "duplicate dependency")
    return set(names)


def list_field(block, name):
    match = re.findall(r"^\s+" + re.escape(name) + r":\s*\[([^\]]+)\]\s*$", block, re.M)
    require(len(match) == 1, "missing matrix axis: " + name)
    values = [value.strip().strip("\"'") for value in match[0].split(",")]
    require(len(values) == len(set(values)), "duplicate matrix value")
    return set(values)


def verify_action(block, expected):
    steps = re.split(r"(?=^      - )", block, flags=re.M)
    verifiers = [step for step in steps if re.search(r"^\s+mode: verify\s*$", step, re.M)]
    require(len(verifiers) == 1, "required gate must have one verifier")
    require(re.search(r"^      - uses: (?:\./\.github/actions/ci-policy|WingedGuardian/modding-agent-harness/\.github/actions/ci-policy@[0-9a-f]{40})\s*$", verifiers[0], re.M) is not None,
            "required gate must execute the shared verifier")
    block = verifiers[0]
    match = re.findall(r"^\s+required-jobs:\s*'\[([^\]]*)\]'\s*$", block, re.M)
    require(len(match) == 1, "required gate has no explicit job inventory")
    names = re.findall(r'"([a-zA-Z][a-zA-Z0-9_-]*)"', match[0])
    require(set(names) == expected and len(names) == len(expected), "gate inventory differs from required jobs")
    require("needs-json: ${{ toJSON(needs) }}" in block, "gate must verify actual dependency results")


def check_contract(root, profile):
    require(profile in FAST, "unknown workflow profile")
    root = Path(root)
    main = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    full = (root / ".github/workflows/full-qualification.yml").read_text(encoding="utf-8")
    outer, inner = jobs(main), jobs(full)
    require(set(outer) == FAST[profile] | {"policy", "qualification", "required"}, "outer job inventory changed")
    require(set(inner) == FULL[profile] | {"full-required"}, "full job inventory changed")
    require(re.search(r"^  workflow_call:\s*$", full, re.M) is not None, "full workflow must be called by its fast gate")
    require(not re.search(r"^  (push|pull_request|workflow_dispatch):", full, re.M), "full workflow bypasses the fast gate")
    require("ready_for_review" in main and "synchronize" in main, "ready or update trigger missing")
    require(re.search(r"^  workflow_dispatch:\s*$", main, re.M) is not None, "manual full qualification missing")
    require(not re.search(r"^  pull_request_target:", main, re.M), "privileged PR trigger forbidden")
    require("ci/**" not in main, "feature pushes must use the PR path")
    for name in FAST[profile]:
        require(needed(outer[name]) == {"policy"}, "fast checks must follow policy classification")
    require("profile: " + profile in outer["policy"], "policy profile missing")
    call = outer["qualification"]
    require(field(call, "uses") == "./.github/workflows/full-qualification.yml", "qualification must use its local workflow")
    require(field(call, "if") == FULL_IF, "qualification readiness condition changed")
    require(needed(call) == FAST[profile] | {"policy"}, "full qualification bypasses fast checks")
    require("      group: full-qualification-${{ github.repository }}" in call, "full queue must be repository-wide")
    require("      queue: max" in call and "      cancel-in-progress: false" in call, "full queue must retain pending and running requests")
    require(field(outer["required"], "if") == REQUIRED_IF, "ready PR required gate must always run")
    expected = FAST[profile] | {"policy", "qualification"}
    require(needed(outer["required"]) == expected, "final gate dependency inventory changed")
    verify_action(outer["required"], expected)
    require(field(inner["full-required"], "if") == "${{ always() }}", "full gate must run after failure or skip")
    require(needed(inner["full-required"]) == FULL[profile], "full gate dependency inventory changed")
    verify_action(inner["full-required"], FULL[profile])
    for name in FULL[profile]:
        require(not re.search(r"^    if:", inner[name], re.M), "full matrix job may not be conditionally omitted")
    for text, inventory in ((main, outer), (full, inner)):
        require(not re.search(r"cancel-in-progress:\s*true", text), "automatic cancellation is disabled")
        for name, block in inventory.items():
            if re.search(r"^    uses:", block, re.M):
                require(name == "qualification" and not re.search(r"^    timeout-minutes:", block, re.M), "invalid reusable-call job")
            else:
                limit = field(block, "timeout-minutes")
                require(limit.isdecimal() and 0 < int(limit) <= 360, "unbounded runner job")
        uses = re.findall(r"^\s*-?\s*uses:\s*(\S+)", text, re.M)
        require(bool(uses), "workflow contains no executable actions")
        for action in uses:
            require(action.startswith("./") or re.fullmatch(r"[^@]+@[0-9a-f]{40}", action) is not None, "external action is not SHA-pinned")
        policy_actions = [value for value in uses if "/ci-policy" in value]
        require(bool(policy_actions), "shared CI policy is not used")
        require(profile == "core" or all(value.startswith("WingedGuardian/modding-agent-harness/") for value in policy_actions), "consumer policy must come from core")
    matrix = inner["stdlib" if profile == "core" else "behavior" if profile == "skyrim" else "tests"]
    parallel = re.findall(r"^\s+max-parallel:\s*(\d+)\s*$", matrix, re.M)
    require(len(parallel) == 1 and 1 <= int(parallel[0]) <= 4, "heavy matrix is not capped")
    if profile in ("core", "skyrim"):
        require(list_field(matrix, "os") == {"ubuntu-latest", "windows-latest"}, "OS matrix changed")
        require(list_field(matrix, "python") == {"3.10", "3.11", "3.12", "3.13"}, "Python matrix changed")
    if profile == "skyrim":
        require(list_field(matrix, "part") == {"suite", "mutations"}, "suite or mutation matrix removed")
    if profile == "x4":
        require("os: windows-latest" in matrix and "os: ubuntu-latest" in matrix, "X4 OS matrix changed")
        require("experimental: true" not in matrix and matrix.count("experimental: false") == 2, "X4 platform gate demoted")
    return dict(profile=profile, fast_jobs=sorted(FAST[profile]), full_jobs=sorted(FULL[profile]))
