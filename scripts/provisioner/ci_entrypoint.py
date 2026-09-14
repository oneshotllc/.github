#!/usr/bin/env python3
"""Entry point provision-site.yml's CI job runs on ubuntu-latest. Handles
every provisioning step that GitHub Actions itself can perform: repo
creation, token substitution, Fly app + volume, repo secrets, admin
passphrase, first preview build. Does NOT touch organizations.yaml or
restart the local pipeline service — that file lives only on the local
Mac that runs oneshot_pipeline and is not reachable from a GitHub-hosted
runner; a local step (scripts/provisioner/register_locally.py) does that
half after this job's outputs come back. This split is a real environment
constraint, not a design shortcut — see BRIEF-provisioner.md step 2's
"complete working site system" claim, which spans both halves.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from provision import (  # noqa: E402
    ProvisioningTicket,
    derive_slug,
    file_ticket,
    generate_passphrase,
    step_create_repo,
    step_ensure_fly_app,
    step_set_repo_secrets,
    step_trigger_preview,
)


class CIShell:
    """In CI, GH_TOKEN is already the minted app token (set by the workflow
    step before this script runs) — every `gh` call is a bare `gh`, never
    `oneshot-pr exec gh` (that wrapper is for local sessions only; see its
    own header comment).
    """

    def run(self, argv: list[str], check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(argv, check=check, capture_output=True, text=True)

    def gh(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        return self.run(["gh", *args], check=check)


def write_secret_from_env(repo: str, secret_name: str, env_var: str) -> ProvisioningTicket | None:
    value = os.environ.get(env_var, "")
    if not value:
        return ProvisioningTicket(
            title=f"Missing {env_var} to set {secret_name} on {repo}",
            tried=f"gh secret set {secret_name} --repo {repo} (reading from ${env_var})",
            hit=f"${env_var} was empty in the provision-site.yml job environment",
            need=f"Wire {env_var} into provision-site.yml's secrets: block.",
        )
    proc = subprocess.run(["gh", "secret", "set", secret_name, "--repo", repo], input=value, capture_output=True, text=True)
    if proc.returncode != 0:
        return ProvisioningTicket(
            title=f"Cannot set {secret_name} on {repo}",
            tried=f"gh secret set {secret_name} --repo {repo}",
            hit=(proc.stderr or "non-zero exit").strip(),
            need="Repo admin access via the minted app token to set secrets.",
        )
    return None


def main() -> int:
    org_name = os.environ["ORG_NAME"]
    domain_in = os.environ.get("DOMAIN", "").strip()
    region = os.environ.get("REGION", "ord").strip() or "ord"
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"

    slug = derive_slug(org_name)
    repo = f"oneshotmn/{slug}"
    fly_app = slug
    domain = domain_in or f"{slug}.oneshot.help"

    tickets: list[ProvisioningTicket] = []
    sh = CIShell()

    plan = {"org_name": org_name, "slug": slug, "repo": repo, "fly_app": fly_app, "domain": domain}
    if dry_run:
        print(json.dumps(plan, indent=2))
        _emit_outputs(repo=repo, slug=slug, fly_app=fly_app, tickets_filed=0)
        return 0

    create_ticket = step_create_repo(sh, repo=repo, template_repo="oneshotmn/site-template")
    if create_ticket:
        tickets.append(create_ticket)
        file_ticket(sh, create_ticket)
        _emit_outputs(repo=repo, slug=slug, fly_app=fly_app, tickets_filed=len(tickets))
        raise SystemExit(f"create_repo did not converge for {repo}: {create_ticket.hit}")

    # Token substitution: clone, replace __TOKEN__s, commit + push idempotently.
    clone_dir = Path("/tmp") / f"provision-{slug}"
    if clone_dir.exists():
        subprocess.run(["rm", "-rf", str(clone_dir)])
    token = os.environ["GH_TOKEN"]
    clone_url = "https://x-access-token:" + token + "@github.com/" + repo + ".git"
    clone = subprocess.run(["git", "clone", clone_url, str(clone_dir)], capture_output=True, text=True)
    if clone.returncode != 0:
        print(clone.stderr, file=sys.stderr)
        raise SystemExit(f"git clone of {repo} failed: " + clone.stderr.strip())
    sub = subprocess.run(
        [
            sys.executable, str(Path(__file__).parent / "substitute.py"), str(clone_dir),
            "--org-slug", slug, "--org-name", org_name, "--fly-app", fly_app,
            "--fly-region", region, "--domain", domain, "--theme-tokens", "default",
        ],
        capture_output=True, text=True,
    )
    print(sub.stdout)
    if sub.returncode != 0:
        print(sub.stderr, file=sys.stderr)
        tickets.append(ProvisioningTicket(
            title=f"Token substitution left placeholders in {repo}",
            tried="scripts/provisioner/substitute.py",
            hit=sub.stderr.strip(),
            need="Add the missing templated file to substitute.py's TEMPLATED_FILES.",
        ))
    else:
        subprocess.run(["git", "-C", str(clone_dir), "config", "user.email", "oneshot-pr-bot@users.noreply.github.com"])
        subprocess.run(["git", "-C", str(clone_dir), "config", "user.name", "oneshot-pr-bot"])
        subprocess.run(["git", "-C", str(clone_dir), "add", "-A"])
        diff = subprocess.run(["git", "-C", str(clone_dir), "diff", "--cached", "--quiet"])
        if diff.returncode != 0:  # there are staged changes
            subprocess.run(["git", "-C", str(clone_dir), "commit", "-q", "-m", f"Provision: substitute template tokens for {org_name}"], check=True)
            subprocess.run(["git", "-C", str(clone_dir), "push", "origin", "HEAD:main"], check=True)

    fly_ticket = step_ensure_fly_app(sh, fly_app=fly_app, region=region)
    if fly_ticket:
        tickets.append(fly_ticket)
        file_ticket(sh, fly_ticket)

    passphrase = generate_passphrase()
    passphrase_proc = subprocess.run(["gh", "secret", "set", "WP_ADMIN_PASSPHRASE", "--repo", repo], input=passphrase, capture_output=True, text=True)
    if passphrase_proc.returncode != 0:
        t = ProvisioningTicket(
            title=f"Cannot store admin passphrase for {repo}",
            tried="gh secret set WP_ADMIN_PASSPHRASE --repo <repo>",
            hit=(passphrase_proc.stderr or "non-zero exit").strip(),
            need="Repo admin access to set secrets.",
        )
        tickets.append(t)
        file_ticket(sh, t)

    for secret_name, env_var in (
        ("ONESHOT_PR_APP_ID", "PR_APP_ID_VALUE"),
        ("ONESHOT_PR_PRIVATE_KEY", "PR_APP_PRIVATE_KEY_VALUE"),
        ("FLY_API_TOKEN", "FLY_API_TOKEN"),
    ):
        t = write_secret_from_env(repo, secret_name, env_var)
        if t:
            tickets.append(t)
            file_ticket(sh, t)

    status, preview_ticket = step_trigger_preview(sh, repo=repo)
    if preview_ticket:
        tickets.append(preview_ticket)
        file_ticket(sh, preview_ticket)

    print(json.dumps({**plan, "tickets": [t.title for t in tickets]}, indent=2))
    _emit_outputs(repo=repo, slug=slug, fly_app=fly_app, tickets_filed=len(tickets))
    return 0


def _emit_outputs(*, repo: str, slug: str, fly_app: str, tickets_filed: int) -> None:
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if not gh_out:
        return
    with open(gh_out, "a", encoding="utf-8") as f:
        f.write(f"repo={repo}\n")
        f.write(f"slug={slug}\n")
        f.write(f"fly_app={fly_app}\n")
        f.write(f"tickets_filed={tickets_filed}\n")


if __name__ == "__main__":
    raise SystemExit(main())
