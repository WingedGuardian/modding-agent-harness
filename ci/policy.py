"""Shared CI policy. This development tool is not part of the runtime package."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

VERSION = 1
PROFILES = frozenset(("core", "skyrim", "x4"))
SHA = re.compile(r"[0-9a-f]{40}\Z")
JOB = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]*\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
PR_ACTIONS = frozenset(("opened", "reopened", "synchronize", "ready_for_review", "converted_to_draft"))


class PolicyError(ValueError):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError("duplicate JSON key")
        result[key] = value
    return result


def read_json(text):
    if len(text) > 2_000_000:
        raise PolicyError("CI metadata exceeds the limit")
    try:
        return json.loads(text, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise PolicyError("invalid CI metadata JSON") from exc


def context(profile, event_name, event, candidate_sha, repository):
    if profile not in PROFILES:
        raise PolicyError("unknown CI profile")
    if not isinstance(candidate_sha, str) or not SHA.fullmatch(candidate_sha):
        raise PolicyError("missing or invalid tested commit")
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise PolicyError("missing or invalid repository identity")
    if not isinstance(event, dict):
        raise PolicyError("event must be an object")
    head, base, full = candidate_sha, "", True
    if event_name == "pull_request":
        if event.get("action") not in PR_ACTIONS:
            raise PolicyError("unsupported PR event")
        pr = event.get("pull_request")
        if not isinstance(pr, dict) or type(pr.get("draft")) is not bool:
            raise PolicyError("missing PR readiness state")
        if type(pr.get("number")) is not int or pr["number"] < 1:
            raise PolicyError("missing PR identity")
        try:
            head, base = pr["head"]["sha"], pr["base"]["sha"]
        except (KeyError, TypeError) as exc:
            raise PolicyError("missing PR source identities") from exc
        if not all(isinstance(value, str) and SHA.fullmatch(value) for value in (head, base)):
            raise PolicyError("invalid PR source identities")
        full = not pr["draft"]
    elif event_name not in ("push", "workflow_dispatch"):
        raise PolicyError("unsupported workflow event")
    return dict(schema=VERSION, profile=profile, repository=repository, event=event_name,
                full_required=full, head_sha=head, candidate_sha=candidate_sha, base_sha=base,
                qualification="required" if full else "draft-fast-checks-only")


def verify(required, results):
    if not isinstance(required, list) or not 1 <= len(required) <= 256:
        raise PolicyError("required job inventory must be nonempty")
    if any(not isinstance(name, str) or not JOB.fullmatch(name) for name in required):
        raise PolicyError("invalid required job identity")
    if len(set(required)) != len(required):
        raise PolicyError("duplicate required job identity")
    if not isinstance(results, dict) or set(results) != set(required):
        raise PolicyError("required job inventory differs from dependency results")
    statuses = {}
    for name in required:
        row = results[name]
        if not isinstance(row, dict) or row.get("result") != "success":
            raise PolicyError("required job did not succeed: " + name)
        statuses[name] = "success"
    return statuses


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def elapsed(start, end):
    a, b = timestamp(start), timestamp(end)
    return max(0, (b-a).total_seconds()) if a and b else None


def timing_rows(jobs):
    rows = []
    for job in jobs:
        rows.append(dict(id=job["id"], name=job["name"], run_attempt=job.get("run_attempt"),
                         status=job["status"], conclusion=job.get("conclusion"),
                         queued_seconds=elapsed(job.get("created_at"), job.get("started_at")),
                         execution_seconds=elapsed(job.get("started_at"), job.get("completed_at"))))
    return rows


def collect_timings(repository, run_id, token, opener=urllib.request.urlopen):
    if not REPOSITORY.fullmatch(repository) or not run_id.isdecimal():
        raise PolicyError("invalid timing request identity")
    all_jobs = []
    for page in range(1, 21):
        url = "https://api.github.com/repos/{}/actions/runs/{}/jobs?filter=all&per_page=100&page={}".format(
            repository, run_id, page)
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "modding-agent-harness-ci"}
        if token:
            headers["Authorization"] = "Bearer " + token
        with opener(urllib.request.Request(url, headers=headers), timeout=20) as response:
            data = read_json(response.read(4_000_001).decode("utf-8"))
        jobs = data["jobs"]
        all_jobs.extend(jobs)
        if len(jobs) < 100:
            return timing_rows(all_jobs)
    raise PolicyError("timing job inventory exceeds pagination limit")


def write_record(path, record):
    Path(path).write_text(json.dumps(record, indent=2)+"\n", encoding="utf-8", newline="\n")


def summary(text):
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if destination:
        with open(destination, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(text+"\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("context", "verify", "report", "contract"))
    parser.add_argument("--profile", required=True, choices=sorted(PROFILES))
    args = parser.parse_args(argv)
    if args.mode == "contract":
        from contracts import check_contract, ContractError
        try:
            print(json.dumps(check_contract(Path(os.environ.get("GITHUB_WORKSPACE", ".")), args.profile)))
            return 0
        except (OSError, ContractError) as exc:
            print("CI workflow contract refused: " + str(exc), file=sys.stderr)
            return 1
    directory = Path(os.environ["RUNNER_TEMP"])
    policy_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    try:
        event = read_json(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        record = context(args.profile, os.environ["GITHUB_EVENT_NAME"], event,
                         os.environ["GITHUB_SHA"], os.environ["GITHUB_REPOSITORY"])
        record.update(policy_sha256=policy_hash, policy_ref=os.environ.get("CI_POLICY_REF") or "local-candidate",
                      run_id=os.environ.get("GITHUB_RUN_ID"), run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT"))
        if args.mode == "context":
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8", newline="\n") as stream:
                for name in ("full_required", "head_sha", "candidate_sha", "base_sha"):
                    value = record[name]
                    stream.write(name+"="+(str(value).lower() if isinstance(value, bool) else value)+"\n")
            summary("**CI policy:** " + record["qualification"] + "\n\nHead: `" + record["head_sha"] +
                    "`; tested candidate: `" + record["candidate_sha"] + "`.")
        elif args.mode == "verify":
            record["jobs"] = verify(read_json(os.environ.get("CI_REQUIRED_JOBS", "null")),
                                    read_json(os.environ.get("CI_NEEDS_JSON", "null")))
            record["qualification"] = "successful-required-jobs"
            summary("**Required jobs:** all " + str(len(record["jobs"])) + " succeeded at `" + record["candidate_sha"] + "`.")
        else:
            try:
                record["job_timings"] = collect_timings(record["repository"], os.environ["GITHUB_RUN_ID"],
                                                        os.environ.get("CI_GITHUB_TOKEN", ""))
                summary("| Job | Attempt | Queue seconds | Execution seconds |\n|---|---:|---:|---:|")
                for row in record["job_timings"]:
                    name = row["name"].replace("|", "\\|").replace("\n", " ")
                    summary("| {} | {} | {} | {} |".format(name, row["run_attempt"],
                        "unknown" if row["queued_seconds"] is None else round(row["queued_seconds"], 1),
                        "in progress/unknown" if row["execution_seconds"] is None else round(row["execution_seconds"], 1)))
            except (OSError, KeyError, TypeError, PolicyError) as exc:
                # Observability is not an alternative qualification verdict.
                record["timing_unavailable"] = type(exc).__name__
                summary("Timing metadata unavailable; consult per-job logs. Qualification checks are unchanged.")
        write_record(directory / ("ci-" + args.mode + ".json"), record)
        return 0
    except (OSError, KeyError, PolicyError) as exc:
        summary("**CI policy refused:** " + (str(exc) if isinstance(exc, PolicyError) else type(exc).__name__))
        print("CI policy refused: " + (str(exc) if isinstance(exc, PolicyError) else type(exc).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
