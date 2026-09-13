#!/usr/bin/env python3
"""Run on the local Mac after provision-site.yml's job finishes (GitHub
Actions cannot reach organizations.yaml or launchctl — see
ci_entrypoint.py's docstring for why this half is split out). Registers the
new repo in oneshot-pipeline's config and kicks the pipeline service so it
picks up the new organization without a full restart.

Usage: register_locally.py <org_id> <org_name> <repo>
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from provision import step_register_organization  # noqa: E402

DEFAULT_ORGANIZATIONS_PATH = Path(
    "/Users/oneshot-agent/code/oneshot-pipeline/config/organizations.yaml"
)
LAUNCHD_LABEL = "com.oneshot.pipeline"


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 3:
        print("usage: register_locally.py <org_id> <org_name> <repo>", file=sys.stderr)
        return 2
    org_id, org_name, repo = argv
    step_register_organization(
        organizations_path=DEFAULT_ORGANIZATIONS_PATH, org_id=org_id, org_name=org_name, repo=repo
    )
    kick = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/501/{LAUNCHD_LABEL}"],
        capture_output=True, text=True,
    )
    if kick.returncode != 0:
        print(f"register_locally.py: launchctl kickstart failed (non-fatal): {kick.stderr.strip()}", file=sys.stderr)
    print(f"register_locally.py: {org_id} -> {repo} recorded in {DEFAULT_ORGANIZATIONS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
