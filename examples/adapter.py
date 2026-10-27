"""Adapter starting point: replace policy/profile imports with the toolkit's.

Translate the real host payload to a v1 Action before calling handle. Preserve
the actual shell/cwd/session/call identities when supplied. Do not invent IDs
and present them as host evidence. Render result.decision using a hard-block
transport qualified on that host version; JSON or exit status alone is not
proof that a host will block. Test failures and unreviewed/changed hooks too.
"""
import json
import sys
from pathlib import Path

from modding_harness.generic import file_policy, handle
from modding_harness.profiles import toy


def pre(raw_action, workspace, save_roots, local_restrictions=()):
    profile = toy(save_roots)
    return handle(raw_action, workspace, profile, file_policy(profile), local_restrictions)


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve()
    result = pre(sys.stdin.buffer.read(1024 * 1024 + 1), root, [root / "saves"])
    print(json.dumps(result))
