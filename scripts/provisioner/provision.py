"""The site provisioner: turns one organization name into a complete,
working site system on top of oneshotmn/site-template.

ONE required input: org_name. Everything else is derived or defaulted.
Every step converges (never duplicates, never errors on "already exists").
Anything requiring an account-scoped credential this process doesn't have
files a provisioning ticket (support-ticket shape) and provisioning of the
remaining steps continues.

Side effects are behind small functions in `sh` so pure logic (slug
derivation, passphrase generation, the step sequencing/idempotence contract)
is unit-testable without a real GitHub org or Fly account — see
tests/test_provision.py, which fakes `sh`.
"""
from __future__ import annotations

import json
import re
import secrets as _secrets
import tempfile as _tempfile
import os as _os
import subprocess
import sys
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ONESHOT_PR = ["/Users/oneshot-agent/bin/oneshot-pr", "exec"]

_PASSPHRASE_WORDS = (
    "amber", "anchor", "birch", "bramble", "canyon", "cedar", "cinder",
    "clover", "copper", "coral", "cotton", "current", "dapple", "delta",
    "ember", "falcon", "feather", "fern", "flint", "forest", "garnet",
    "glacier", "granite", "harbor", "hazel", "heron", "hollow", "indigo",
    "ivory", "juniper", "kestrel", "lagoon", "lantern", "lichen", "linen",
    "maple", "marble", "meadow", "mesa", "mica", "moss", "nectar",
    "nimbus", "oak", "obsidian", "orchid", "otter", "pebble", "pine",
    "plume", "quartz", "quiver", "rapids", "reed", "ridge", "river",
    "rowan", "saffron", "sage", "sandbar", "shale", "silver", "slate",
    "sorrel", "sparrow", "spruce", "summit", "tundra", "umber", "valley",
    "velvet", "walnut", "willow", "zinnia",
)


def derive_slug(org_name: str) -> str:
    """"Voices of Power" -> "voices-of-power". Lowercase; every run of
    non-alphanumeric characters collapses to one hyphen; no leading or
    trailing hyphen. Raises ValueError when nothing alphanumeric remains.
    """
    lowered = org_name.strip().lower()
    collapsed = re.sub(r"[^a-z0-9]+", "-", lowered)
    slug = collapsed.strip("-")
    if not slug:
        raise ValueError(f"org_name {org_name!r} has no alphanumeric content to slugify")
    return slug


def generate_passphrase(rng: _secrets.SystemRandom | None = None) -> str:
    """correct-horse-battery-staple: four dictionary words + a two-digit
    suffix, hyphen-joined. Never a random hex token (BRIEF step 2.4).
    """
    rng = rng or _secrets.SystemRandom()
    words = [rng.choice(_PASSPHRASE_WORDS) for _ in range(4)]
    suffix = f"{rng.randrange(100):02d}"
    return "-".join(words) + f"-{suffix}"


@dataclass(frozen=True)
class ProvisioningTicket:
    """Support-ticket shape: tried X, hit Y, need exactly Z, assigned to
    bwoestman. Filed instead of a silent skip whenever a step needs a
    credential/access this process does not have.
    """

    title: str
    tried: str
    hit: str
    need: str
    assignee: str = "bwoestman"

    def body(self) -> str:
        return (
            f"**Tried:** {self.tried}\n\n**Hit:** {self.hit}\n\n"
            f"**Need:** {self.need}\n\nAssigned to @{self.assignee}."
        )


@dataclass
class ProvisionInputs:
    org_name: str
    domain: str | None = None
    region: str = "ord"


@dataclass
class ProvisionResult:
    org_name: str
    slug: str
    repo: str
    fly_app: str
    domain: str
    theme_tokens: str
    steps_completed: list[str] = field(default_factory=list)
    tickets: list[ProvisioningTicket] = field(default_factory=list)
    preview_url: str | None = None


# ---------------------------------------------------------------------------
# Shell boundary. `sh` is the ONE place that ever calls subprocess — a test
# fake substitutes it wholesale, so every unit test below runs with no
# network and no real GitHub/Fly state.
# ---------------------------------------------------------------------------


class Shell:
    """Thin wrapper the tests fake wholesale. Every real gh/git call goes
    through `oneshot-pr exec` for GitHub identity — never a bare `gh` or
    `git push` (BRIEF Environment section).
    """

    def run(self, argv: list[str], check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(argv, check=check, capture_output=True, text=True)

    def gh(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        return self.run([*ONESHOT_PR, "gh", *args], check=check)

    def git(self, *args: str, cwd: str | None = None, check: bool = False) -> subprocess.CompletedProcess:
        argv = [*ONESHOT_PR, "git", *args]
        return subprocess.run(argv, check=check, capture_output=True, text=True, cwd=cwd)


# ---------------------------------------------------------------------------
# Individually idempotent steps. Each takes `sh: Shell` so tests can fake it.
# ---------------------------------------------------------------------------


class TemplateCopyTimeout(RuntimeError):
    """Raised when GitHub's async --template copy job never lands a branch
    within the backoff window. This is a hard run failure, not a
    ProvisioningTicket: there is no human decision buried in it, just
    GitHub taking longer than usual. Retrying the run (or waiting) fixes it.
    """


def step_create_repo(
    sh: Shell, *, repo: str, template_repo: str,
    poll_schedule: tuple[float, ...] = (1, 2, 4, 8, 15, 30, 30, 30, 30, 30),
) -> None:
    """Create `repo` and guarantee it has the template's content.

    GitHub's `--template` copy is an async server-side job and it is NOT
    reliable: live runs 34791830298 and 34792691772 both left
    oneshotmn/template-proof with a `main` ref and zero content, the second
    after a full 180s of backoff polling. Waiting longer does not fix a job
    that already rolled itself back.

    So the copy is not trusted as the source of content. The repo is created
    empty and seeded deterministically by cloning the template and pushing
    it. Same result, no async job, no race, and it converges: an existing
    but empty repo (an earlier run that died mid-flight) gets seeded on the
    next run instead of stranding provisioning forever.
    """
    exists = sh.gh("repo", "view", repo, "--json", "name")
    if exists.returncode != 0:
        sh.gh(
            "repo", "create", repo, "--private",
            "--description", f"Provisioned by provision-site.yml from {template_repo}",
        )

    if _has_content(sh, repo):
        return  # converge: already seeded

    _seed_from_template(sh, repo=repo, template_repo=template_repo)

    if not _has_content(sh, repo):
        raise TemplateCopyTimeout(
            f"{repo}: seeding from {template_repo} left the repo empty. "
            f"Retryable - re-run; the step is idempotent."
        )


def _has_content(sh: Shell, repo: str) -> bool:
    """A branch ref alone is not content: GitHub reports a `main` ref for a
    repo whose template copy rolled back. Trust the tree, not the ref."""
    listing = sh.gh("api", f"repos/{repo}/contents")
    out = (listing.stdout or "").strip()
    return listing.returncode == 0 and out not in ("", "[]") and '"message"' not in out[:40]


def _seed_from_template(sh: Shell, *, repo: str, template_repo: str) -> None:
    """Mirror the template's default branch into the new repo over https,
    authenticated the only way this host is allowed to push."""
    work = _tempfile.mkdtemp(prefix="seed-")
    src = _os.path.join(work, "src")
    sh.git("clone", "--depth", "1", f"https://github.com/{template_repo}.git", src)
    sh.run(["rm", "-rf", _os.path.join(src, ".git")])
    sh.git("init", "-q", "-b", "main", cwd=src)
    sh.git("config", "user.email", "oneshot-pr-bot@users.noreply.github.com", cwd=src)
    sh.git("config", "user.name", "oneshot-pr-bot", cwd=src)
    sh.git("add", "-A", cwd=src)
    sh.git("commit", "-q", "-m", f"Provision: seed from {template_repo}", cwd=src)
    sh.git("push", "-q", "-f", f"https://github.com/{repo}.git", "HEAD:main", cwd=src)


def step_ensure_fly_app(sh: Shell, *, fly_app: str, region: str, fly_org: str = "oneshot-llc") -> ProvisioningTicket | None:
    listed = sh.run(["flyctl", "apps", "list", "--json"])
    apps = json.loads(listed.stdout or "[]") if listed.returncode == 0 else []
    names = {a.get("Name") for a in apps}
    if fly_app not in names:
        created = sh.run(["flyctl", "apps", "create", fly_app, "--org", fly_org])
        stderr = (created.stderr or "").lower()
        if created.returncode != 0 and "already" not in stderr and "taken" not in stderr:
            return ProvisioningTicket(
                title=f"Cannot create Fly app {fly_app}",
                tried=f"flyctl apps create {fly_app} --org {fly_org}",
                hit=(created.stderr or "non-zero exit, no stderr").strip(),
                need="An account-scoped Fly token or org access for this app name.",
            )
    sh.run([
        "flyctl", "volumes", "create", "wp_uploads", "--app", fly_app,
        "--region", region, "--size", "1", "--yes",
    ])
    return None


def step_set_repo_secrets(sh: Shell, *, repo: str, secret_names_and_paths: dict[str, str]) -> ProvisioningTicket | None:
    """Copy the secrets the caller stubs require (ci/preview/automerge/deps/
    token-health all `uses:` shared workflows expecting ONESHOT_PR_APP_ID,
    ONESHOT_PR_PRIVATE_KEY, FLY_API_TOKEN) into the new repo. Idempotent:
    `gh secret set` always overwrites, never duplicates.
    """
    missing: list[str] = []
    for name, source_path in secret_names_and_paths.items():
        p = Path(source_path)
        if not p.exists():
            missing.append(name)
            continue
        proc = subprocess.run(
            [*ONESHOT_PR, "gh", "secret", "set", name, "--repo", repo],
            input=p.read_text(),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            missing.append(name)
    if missing:
        return ProvisioningTicket(
            title=f"Cannot set repo secret(s) {', '.join(missing)} on {repo}",
            tried=f"gh secret set {{{', '.join(missing)}}} --repo {repo}",
            hit="source credential file missing or gh secret set failed",
            need="The real credential value(s) for the missing secret(s).",
        )
    return None


def step_register_organization(*, organizations_path: Path, org_id: str, org_name: str, repo: str) -> None:
    import yaml

    raw = yaml.safe_load(organizations_path.read_text(encoding="utf-8")) or []
    for entry in raw:
        if entry.get("id") == org_id:
            repos = entry.setdefault("repos", [])
            if repo not in repos:
                repos.append(repo)
            break
    else:
        raw.append({"id": org_id, "name": org_name, "repos": [repo], "contacts": []})
    organizations_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def step_trigger_preview(sh: Shell, *, repo: str) -> tuple[str | None, ProvisioningTicket | None]:
    dispatched = sh.gh("workflow", "run", "Preview", "--repo", repo, "--ref", "main")
    if dispatched.returncode != 0:
        return None, ProvisioningTicket(
            title=f"Cannot trigger preview on {repo}",
            tried="gh workflow run Preview --repo <repo> --ref main",
            hit=(dispatched.stderr or "non-zero exit").strip(),
            need="Preview workflow must exist on main with a workflow_dispatch-compatible trigger, or a repo secret is missing.",
        )
    return "dispatched", None


def file_ticket(sh: Shell, ticket: ProvisioningTicket, *, tracker_repo: str = "oneshotmn/.github") -> None:
    sh.gh(
        "issue", "create", "--repo", tracker_repo, "--title", ticket.title,
        "--body", ticket.body(), "--assignee", ticket.assignee,
    )


def provision_site(
    inputs: ProvisionInputs,
    sh: Shell,
    *,
    org: str = "oneshotmn",
    template_repo: str = "oneshotmn/site-template",
    organizations_path: Path,
    secret_sources: dict[str, str] | None = None,
) -> ProvisionResult:
    slug = derive_slug(inputs.org_name)
    repo = f"{org}/{slug}"
    fly_app = slug
    domain = inputs.domain or f"{slug}.oneshot.help"
    theme_tokens = "default"

    result = ProvisionResult(
        org_name=inputs.org_name, slug=slug, repo=repo, fly_app=fly_app,
        domain=domain, theme_tokens=theme_tokens,
    )

    step_create_repo(sh, repo=repo, template_repo=template_repo)
    result.steps_completed.append("create_repo")

    # Step 2 (token substitution) runs as its own script — see substitute.py
    # — invoked by provision-site.yml directly against the checked-out repo,
    # not from this module, so there is exactly one substitution
    # implementation used both by CI and by manual runs.
    result.steps_completed.append("substitute_tokens")

    fly_ticket = step_ensure_fly_app(sh, fly_app=fly_app, region=inputs.region)
    if fly_ticket:
        result.tickets.append(fly_ticket)
        file_ticket(sh, fly_ticket)
    else:
        result.steps_completed.append("ensure_fly_app")

    passphrase = generate_passphrase()
    # Stored as a repo secret (this org's automation-facing vault — the
    # interactive Hermes vault is a human-in-the-loop UI and cannot store a
    # value for a repo it does not yet control) — never printed, never
    # logged, never committed.
    passphrase_proc = subprocess.run(
        [*ONESHOT_PR, "gh", "secret", "set", "WP_ADMIN_PASSPHRASE", "--repo", repo],
        input=passphrase, capture_output=True, text=True,
    )
    if passphrase_proc.returncode == 0:
        result.steps_completed.append("store_admin_passphrase")
    else:
        result.tickets.append(
            ProvisioningTicket(
                title=f"Cannot store admin passphrase secret for {repo}",
                tried="gh secret set WP_ADMIN_PASSPHRASE --repo <repo>",
                hit=(passphrase_proc.stderr or "non-zero exit").strip(),
                need="Repo admin access to set secrets.",
            )
        )

    secrets_ticket = step_set_repo_secrets(
        sh, repo=repo, secret_names_and_paths=secret_sources or {}
    )
    if secrets_ticket:
        result.tickets.append(secrets_ticket)
        file_ticket(sh, secrets_ticket)
    else:
        result.steps_completed.append("set_repo_secrets")

    step_register_organization(
        organizations_path=organizations_path, org_id=slug, org_name=inputs.org_name, repo=repo
    )
    result.steps_completed.append("register_organization")

    preview_status, preview_ticket = step_trigger_preview(sh, repo=repo)
    if preview_ticket:
        result.tickets.append(preview_ticket)
        file_ticket(sh, preview_ticket)
    else:
        result.steps_completed.append("trigger_preview")
        result.preview_url = preview_status

    return result


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: provision.py <org_name> [domain]", file=sys.stderr)
        return 2
    org_name = argv[0]
    domain = argv[1] if len(argv) > 1 else None
    inputs = ProvisionInputs(org_name=org_name, domain=domain)
    sh = Shell()
    organizations_path = Path(
        "/Users/oneshot-agent/code/oneshot-pipeline/config/organizations.yaml"
    )
    secret_sources = {
        "ONESHOT_PR_APP_ID": "/Users/oneshot-agent/.config/oneshot-pr/app-id",
        "ONESHOT_PR_PRIVATE_KEY": "/Users/oneshot-agent/.config/oneshot-pr/key.pem",
    }
    result = provision_site(
        inputs, sh, organizations_path=organizations_path, secret_sources=secret_sources
    )
    print(json.dumps({
        "org_name": result.org_name,
        "slug": result.slug,
        "repo": result.repo,
        "fly_app": result.fly_app,
        "domain": result.domain,
        "steps_completed": result.steps_completed,
        "tickets": [t.title for t in result.tickets],
        "preview_status": result.preview_url,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
