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

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from provision import (  # noqa: E402
    HttpClient,
    ProvisioningTicket,
    derive_org_name_from_domain,
    derive_slug_from_domain,
    file_ticket,
    generate_passphrase,
    poll_until_200,
    step_create_repo,
    step_ensure_cert,
    step_ensure_dns_record,
    step_ensure_fly_app,
    step_deploy_image,
    step_set_fly_secrets,
    step_set_repo_secrets,
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

    def git(self, *args: str, cwd: str | None = None, check: bool = False) -> subprocess.CompletedProcess:
        """Seeding clones and pushes over https. In CI the app token in
        GH_TOKEN is the only credential, so it is injected per-call via an
        ephemeral header rather than written into any URL or config (it must
        never reach a log or the pushed repo)."""
        token = os.environ.get("GH_TOKEN", "")
        argv = ["git"]
        if token:
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            argv += ["-c", f"http.https://github.com/.extraheader=Authorization: Basic {basic}"]
        return subprocess.run([*argv, *args], check=check, capture_output=True, text=True, cwd=cwd)


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
    domain = os.environ["DOMAIN"].strip()
    org_name = os.environ.get("ORG_NAME", "").strip() or derive_org_name_from_domain(domain)
    region = os.environ.get("REGION", "ord").strip() or "ord"
    image_ref = os.environ.get("IMAGE_REF", "").strip()
    cloudflare_token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    cloudflare_zone_id = os.environ.get("CLOUDFLARE_ZONE_ID", "").strip()
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"

    slug = derive_slug_from_domain(domain)
    repo = f"oneshotmn/{slug}"
    fly_app = slug

    tickets: list[ProvisioningTicket] = []
    sh = CIShell()

    plan = {"org_name": org_name, "slug": slug, "repo": repo, "fly_app": fly_app, "domain": domain}
    if dry_run:
        print(json.dumps(plan, indent=2))
        _emit_outputs(repo=repo, slug=slug, fly_app=fly_app, tickets_filed=0, live_url=None)
        return 0

    step_create_repo(sh, repo=repo, template_repo="oneshotmn/site-template")

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

    fly_secrets_ticket = step_set_fly_secrets(
        sh, fly_app=fly_app,
        secrets={
            "WP_ADMIN_PASSPHRASE": passphrase,
            "WP_HOME": f"https://{domain}",
            "WP_SITE_TITLE": org_name,
        },
    )
    if fly_secrets_ticket:
        tickets.append(fly_secrets_ticket)
        file_ticket(sh, fly_secrets_ticket)

    live_url: str | None = None
    if image_ref:
        deploy_ticket = step_deploy_image(sh, fly_app=fly_app, image_ref=image_ref, region=region)
        if deploy_ticket:
            tickets.append(deploy_ticket)
            file_ticket(sh, deploy_ticket)
    else:
        tickets.append(ProvisioningTicket(
            title=f"No IMAGE_REF provided to deploy {fly_app}",
            tried="step_deploy_image with $IMAGE_REF",
            hit="IMAGE_REF was empty in the provision-site.yml job environment",
            need="Wire IMAGE_REF (the published site image tag) into provision-site.yml's env.",
        ))

    if cloudflare_token and cloudflare_zone_id:
        http = HttpClient("https://api.cloudflare.com/client/v4", cloudflare_token)
        zone_lookup = {_registrable_domain_local(domain): cloudflare_zone_id, domain: cloudflare_zone_id}
        dns_ticket = step_ensure_dns_record(http, domain=domain, target=f"{fly_app}.fly.dev", zone_lookup=zone_lookup)
        if dns_ticket:
            tickets.append(dns_ticket)
            file_ticket(sh, dns_ticket)

        cert_ticket = step_ensure_cert(sh, fly_app=fly_app, domain=domain)
        if cert_ticket:
            tickets.append(cert_ticket)
            file_ticket(sh, cert_ticket)
    else:
        tickets.append(ProvisioningTicket(
            title=f"No Cloudflare credentials to point {domain} at {fly_app}",
            tried="step_ensure_dns_record with $CLOUDFLARE_API_TOKEN / $CLOUDFLARE_ZONE_ID",
            hit="one or both env vars were empty in the provision-site.yml job environment",
            need="Wire CLOUDFLARE_API_TOKEN and CLOUDFLARE_ZONE_ID into provision-site.yml's secrets/env.",
        ))

    if image_ref:
        reached, elapsed = poll_until_200(_http_status, f"https://{fly_app}.fly.dev/")
        print(f"poll_until_200: reached={reached} elapsed={elapsed:.2f}s")
        if reached:
            live_url = f"https://{fly_app}.fly.dev/"
        else:
            tickets.append(ProvisioningTicket(
                title=f"{fly_app} never reached 200 within the poll window",
                tried=f"poll https://{fly_app}.fly.dev/ with sub-second backoff up to 300s",
                hit=f"still not 200 after {elapsed:.1f}s",
                need="Investigate the Machine's boot log — this is a hard failure, not a missing credential.",
            ))

    print(json.dumps({**plan, "tickets": [t.title for t in tickets], "live_url": live_url}, indent=2))
    _emit_outputs(repo=repo, slug=slug, fly_app=fly_app, tickets_filed=len(tickets), live_url=live_url)
    return 0


def _registrable_domain_local(domain: str) -> str:
    parts = domain.strip().lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def _http_status(url: str) -> int:
    import urllib.request
    import urllib.error

    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def _emit_outputs(*, repo: str, slug: str, fly_app: str, tickets_filed: int, live_url: str | None) -> None:
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if not gh_out:
        return
    with open(gh_out, "a", encoding="utf-8") as f:
        f.write(f"repo={repo}\n")
        f.write(f"slug={slug}\n")
        f.write(f"fly_app={fly_app}\n")
        f.write(f"tickets_filed={tickets_filed}\n")
        f.write(f"live_url={live_url or ''}\n")


if __name__ == "__main__":
    raise SystemExit(main())
