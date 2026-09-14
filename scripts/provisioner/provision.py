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


def derive_fly_app(domain: str) -> str:
    """"oneshot.help" -> "oneshot-help". The whole domain, hyphenated, so the
    name is as globally unique as the domain itself. A bare slug is not:
    Fly app names are unique across all of Fly.io, not just one org."""
    name = re.sub(r"[^a-z0-9]+", "-", domain.strip().lower()).strip("-")
    if not name:
        raise ValueError(f"domain {domain!r} has no alphanumeric content")
    return name


def derive_slug_from_domain(domain: str) -> str:
    """"oneshot.help" -> "oneshot"; "voices-of-power.org" -> "voices-of-power".
    The Fly app name and repo slug both come from the domain's first label
    (not the whole domain — Fly app names can't contain dots), so
    "oneshot.help" and a future "oneshot.com" would collide; that is an
    intentional 1:1 domain-to-slug mapping for this org's flat domain set.
    """
    first_label = domain.strip().lower().split(".")[0]
    return derive_slug(first_label)



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
    """domain is the one required input (BRIEF step 4: "he gives a domain,
    and one run spins the whole thing up"). org_name is an optional display
    name — when omitted it is derived from the domain's first label
    title-cased (oneshot.help -> "Oneshot"), matching how a human would name
    an org after its own domain.
    """

    domain: str
    org_name: str | None = None
    region: str = "ord"

    def __post_init__(self) -> None:
        if not self.domain or not self.domain.strip():
            raise ValueError("domain is required and cannot be empty")


def derive_org_name_from_domain(domain: str) -> str:
    """"oneshot.help" -> "Oneshot"; "voices-of-power.org" -> "Voices Of Power".
    Used only when the caller does not supply an explicit org_name.
    """
    first_label = domain.strip().split(".")[0]
    words = re.split(r"[^a-zA-Z0-9]+", first_label)
    return " ".join(w.capitalize() for w in words if w)



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
        proc = subprocess.run(argv, check=check, capture_output=True, text=True)
        # Every external command is captured, so without this a failing step
        # is invisible in the run log and the provisioner can only guess at
        # the cause (live: three runs blamed a Fly token for three different
        # non-token bugs). Log the command and its result, never the values.
        if argv and argv[0] in ("flyctl", "fly"):
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            print(f"[cmd] {' '.join(argv[:4])} -> rc={proc.returncode}"
                  + (f" | {tail[-1][:180]}" if tail else ""), flush=True)
        return proc

    def gh(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        return self.run([*ONESHOT_PR, "gh", *args], check=check)

    def git(self, *args: str, cwd: str | None = None, check: bool = False) -> subprocess.CompletedProcess:
        argv = [*ONESHOT_PR, "git", *args]
        return subprocess.run(argv, check=check, capture_output=True, text=True, cwd=cwd)


# ---------------------------------------------------------------------------
# Individually idempotent steps. Each takes `sh: Shell` so tests can fake it.
# ---------------------------------------------------------------------------


def _step_err(scope) -> str:
    """Best-effort stderr from whichever subprocess result the step used, so
    a failure names its real cause instead of guessing at a missing token."""
    for key in ("created", "proc", "added", "resp", "result", "deployed", "listed", "out"):
        r = scope.get(key)
        text = getattr(r, "stderr", None) or getattr(r, "stdout", None)
        if text:
            return str(text).strip()[:400]
    return "non-zero exit"


class ProvisioningError(RuntimeError):
    """A run-level failure: loud, retryable, never a human ticket."""


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
    """Mirror the template's default branch into the new repo over https.

    Every git step is checked: a silent failure here used to surface much
    later as the useless "repo is still empty" error (live run 34793408420),
    hiding the actual cause.
    """
    work = _tempfile.mkdtemp(prefix="seed-")
    src = _os.path.join(work, "src")

    def _must(result, what: str):
        if result.returncode != 0:
            raise TemplateCopyTimeout(
                f"{repo}: seeding failed at {what}: "
                f"{(result.stderr or result.stdout or '').strip()[:400]}"
            )
        return result

    _must(sh.git("clone", "--depth", "1", f"https://github.com/{template_repo}.git", src), "clone template")
    sh.run(["rm", "-rf", _os.path.join(src, ".git")])
    _must(sh.git("init", "-q", "-b", "main", cwd=src), "git init")
    sh.git("config", "user.email", "oneshot-pr-bot@users.noreply.github.com", cwd=src)
    sh.git("config", "user.name", "oneshot-pr-bot", cwd=src)
    _must(sh.git("add", "-A", cwd=src), "git add")
    _must(sh.git("commit", "-q", "-m", f"Provision: seed from {template_repo}", cwd=src), "git commit")
    _must(sh.git("push", "-q", "-f", f"https://github.com/{repo}.git", "HEAD:main", cwd=src), "git push")


def step_ensure_fly_app(sh: Shell, *, fly_app: str, region: str, fly_org: str = "oneshot-llc") -> ProvisioningTicket | None:
    listed = sh.run(["flyctl", "apps", "list", "--json"])
    apps = json.loads(listed.stdout or "[]") if listed.returncode == 0 else []
    names = {a.get("Name") for a in apps}
    if fly_app not in names:
        created = sh.run(["flyctl", "apps", "create", fly_app, "--org", fly_org])
        stderr = (created.stderr or "").lower()
        if created.returncode != 0:
            # "taken" must NOT count as success: Fly app names are unique
            # across ALL of Fly, so a name held by a stranger looks exactly
            # like our own existing app. Live run 34800109357 sailed past a
            # taken `oneshot`, then failed on `secrets import` with a bare
            # "unauthorized" - Fly's answer for an app you do not own.
            owned = sh.run(["flyctl", "apps", "list", "--json"])
            names = set()
            if owned.returncode == 0:
                try:
                    names = {a.get("Name") for a in json.loads(owned.stdout or "[]")}
                except ValueError:
                    names = set()
            if fly_app in names:
                pass  # genuinely ours, created by an earlier run
            elif "taken" in stderr or "unique" in stderr:
                raise ProvisioningError(
                    f"Fly app name {fly_app!r} is taken by an app outside this "
                    f"organization. Derive a more specific name from the domain."
                )
            elif "already" not in stderr:
                raise ProvisioningError(
                    f"Cannot create Fly app {fly_app}: " + (_step_err(locals()))
                )
    step_ensure_volume(sh, fly_app=fly_app, region=region, volume_name="wp_uploads")
    return None


def step_ensure_volume(
    sh: Shell, *, fly_app: str, region: str, volume_name: str = "wp_uploads", size_gb: int = 1,
) -> None:
    """Idempotent volume creation. `flyctl volumes create` has no built-in
    dedup — calling it twice makes two same-named volumes, both attachable,
    with no guarantee a Machine gets the one holding prior data. Live
    evidence: three `wp_uploads` volumes accumulated on one app from
    provisioning being run three times by hand before this check existed.
    List first, match on Name, create only when truly absent — running this
    N times must always converge on exactly one volume.
    """
    listed = sh.run(["flyctl", "volumes", "list", "--app", fly_app, "--json"])
    volumes = json.loads(listed.stdout or "[]") if listed.returncode == 0 else []
    existing_names = {v.get("Name") for v in volumes}
    if volume_name in existing_names:
        return  # converge: already have one, never create a second
    sh.run([
        "flyctl", "volumes", "create", volume_name, "--app", fly_app,
        "--region", region, "--size", str(size_gb), "--yes",
    ])


def step_set_fly_secrets(
    sh: Shell, *, fly_app: str, secrets: dict[str, str],
) -> ProvisioningTicket | None:
    """`flyctl secrets set` is idempotent on its own (always overwrites,
    never duplicates a key) — this wraps it only to give a consistent
    ticket shape on failure. Values are piped via stdin through
    `--stage`-free `import`-style NAME=VALUE args is avoided on purpose:
    building a NAME=VALUE argv list would put secret values in process
    argv, visible to anything that can list processes on the runner.
    `flyctl secrets import` reads NAME=VALUE pairs from stdin instead.
    """
    payload = "\n".join(f"{k}={v}" for k, v in secrets.items())
    proc = subprocess.run(
        ["flyctl", "secrets", "import", "--app", fly_app, "--stage"],
        input=payload, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise ProvisioningError(
            f"setting Fly secrets on {fly_app} failed: "
            + (proc.stderr or "non-zero exit").strip()[:400]
        )
    if False:
        raise ProvisioningError(
            f"Cannot set Fly secrets on {fly_app}: " + (_step_err(locals()))
        )
    return None


def step_deploy_image(
    sh: Shell, *, fly_app: str, image_ref: str, region: str,
) -> ProvisioningTicket | None:
    """Deploys a PREBUILT image — never `flyctl deploy` from source, which
    would build during provisioning and put minutes of image-build time in
    what should be a sub-minute path (BRIEF: image build time must not sit
    in the provisioning path). The image is built and pushed once by
    publish-image.yml; this step only ever references that existing tag.
    """
    # NOTE: `flyctl deploy` has no --region flag (live run 34798261207 died
    # on "unknown flag: --region" and mis-reported it as a token problem).
    # Region belongs to the app/volume, which step_ensure_fly_app already set.
    deployed = sh.run([
        "flyctl", "deploy", "--app", fly_app, "--image", image_ref,
        "--strategy", "immediate", "--yes",
    ])
    if deployed.returncode != 0:
        # A failed deploy is a RUN failure - the command, the image or the
        # app is wrong, and all three are code. Never a human ticket.
        raise ProvisioningError(
            f"deploy of {image_ref} to {fly_app} failed: "
            + (deployed.stderr or deployed.stdout or "non-zero exit").strip()[:500]
        )
    return None


def step_ensure_dns_record(
    http: "HttpClient", *, domain: str, target: str, zone_lookup: dict[str, str],
) -> ProvisioningTicket | None:
    """Points `domain` at `target` (a Fly app's flycast/anycast hostname,
    e.g. "<fly_app>.fly.dev" via CNAME, or a Fly IP via A/AAAA) through the
    Cloudflare API. Idempotent: looks up any existing record for this exact
    name first and PATCHes it in place rather than POSTing a duplicate —
    running provisioning twice must leave exactly one DNS record for the
    domain, not two competing ones.

    `zone_lookup` maps a registrable domain (e.g. "oneshot.help") to its
    Cloudflare zone ID — the provisioner does not have account-level
    Cloudflare access to list zones itself (BRIEF: the Cloudflare token is
    zone-scoped), so the caller supplies the one zone ID it already knows.
    """
    zone_id = zone_lookup.get(domain) or zone_lookup.get(_registrable_domain(domain))
    if not zone_id:
        raise ProvisioningError(
            f"No Cloudflare zone ID known for {domain}: " + (_step_err(locals()))
        )
    existing = http.get(f"/zones/{zone_id}/dns_records", params={"name": domain, "type": "CNAME"})
    if not existing.get("success"):
        raise ProvisioningError(
            f"Cannot read DNS records for {domain}: " + (_step_err(locals()))
        )
    records = existing.get("result") or []
    body = {"type": "CNAME", "name": domain, "content": target, "proxied": False, "ttl": 60}
    if records:
        record_id = records[0]["id"]
        if records[0].get("content") == target:
            return None  # converge: already points at the right target
        result = http.patch(f"/zones/{zone_id}/dns_records/{record_id}", json=body)
    else:
        result = http.post(f"/zones/{zone_id}/dns_records", json=body)
    if not result.get("success"):
        raise ProvisioningError(
            f"Cannot write DNS record for {domain}: " + (_step_err(locals()))
        )
    return None


def _registrable_domain(domain: str) -> str:
    """"sub.oneshot.help" -> "oneshot.help". Naive last-two-labels split —
    correct for every domain this org actually uses (no multi-part public
    suffixes like .co.uk in play).
    """
    parts = domain.strip().lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def step_ensure_cert(sh: Shell, *, fly_app: str, domain: str) -> ProvisioningTicket | None:
    """`flyctl certs add` is idempotent server-side: adding the same
    hostname twice is a no-op, not a duplicate — Fly's certs API keys on
    hostname, not on an add-call count.
    """
    added = sh.run(["flyctl", "certs", "add", domain, "--app", fly_app])
    stderr = (added.stderr or "").lower()
    if added.returncode != 0 and "already" not in stderr and "exist" not in stderr:
        # Cert failures are code/config, not a human decision.
        raise ProvisioningError(
            f"adding cert for {domain} on {fly_app} failed: "
            + (added.stderr or "non-zero exit").strip()[:400]
        )
    return None


def poll_until_200(
    fetch: Callable[[str], int], url: str, *, timeout_seconds: float = 300,
    poll_schedule: tuple[float, ...] = (0.2, 0.2, 0.5, 0.5, 1, 1, 2, 2, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5),
) -> tuple[bool, float]:
    """Sub-second-first exponential-ish backoff, never a fixed sleep: most
    of the schedule's early steps are 200-500ms so a fast deploy (the
    common case with a prebuilt image) reports success in well under a
    second of polling overhead, while the tail still covers Fly's slower
    cold-start cases up to the timeout. Returns (reached_200, elapsed_seconds).
    """
    start = _time.monotonic()
    i = 0
    while True:
        elapsed = _time.monotonic() - start
        if elapsed >= timeout_seconds:
            return False, elapsed
        try:
            if fetch(url) == 200:
                return True, _time.monotonic() - start
        except Exception:
            pass
        wait = poll_schedule[min(i, len(poll_schedule) - 1)]
        remaining = timeout_seconds - (_time.monotonic() - start)
        _time.sleep(max(0.0, min(wait, remaining)))
        i += 1


class HttpClient:
    """Thin wrapper around Cloudflare's REST API, using only the stdlib so
    the provisioner has no extra runtime dependency. Tests fake this
    wholesale (see FakeHttpClient) the same way Shell is faked for gh/flyctl.
    """

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> dict:
        import urllib.parse
        import urllib.request
        import urllib.error

        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(json_body).encode() if json_body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode())
            except Exception:
                return {"success": False, "errors": [{"message": str(e)}]}

    def get(self, path: str, *, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def post(self, path: str, *, json: dict) -> dict:
        return self._request("POST", path, json_body=json)

    def patch(self, path: str, *, json: dict) -> dict:
        return self._request("PATCH", path, json_body=json)



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
        raise ProvisioningError(
            f"Cannot set repo secret(s) {', '.join(missing)} on {repo}: " + (_step_err(locals()))
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
    image_ref: str | None = None,
    http: "HttpClient | None" = None,
    zone_lookup: dict[str, str] | None = None,
    fetch: Callable[[str], int] | None = None,
    skip_deploy: bool = False,
) -> ProvisionResult:
    """domain-first: `inputs.domain` alone is sufficient (BRIEF step 4).
    org_name is resolved from the domain when not given explicitly, so
    `ProvisionInputs(domain="oneshot.help")` and
    `ProvisionInputs(domain="oneshot.help", org_name="OneShot")` provision
    the identical repo/app/slug — only the display name in repo.yml differs.
    """
    org_name = inputs.org_name or derive_org_name_from_domain(inputs.domain)
    slug = derive_slug_from_domain(inputs.domain)
    repo = f"{org}/{slug}"
    # Fly app names are globally unique, so a bare slug like "oneshot"
    # collides with strangers' apps (live run 34800109357). The full domain
    # is already unique and already ours, so derive from it.
    domain = inputs.domain
    fly_app = derive_fly_app(domain)
    theme_tokens = "default"

    result = ProvisionResult(
        org_name=org_name, slug=slug, repo=repo, fly_app=fly_app,
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
        organizations_path=organizations_path, org_id=slug, org_name=org_name, repo=repo
    )
    result.steps_completed.append("register_organization")

    if skip_deploy:
        return result

    fly_secrets_ticket = step_set_fly_secrets(
        sh, fly_app=fly_app,
        secrets={
            "WP_ADMIN_PASSPHRASE": passphrase,
            "WP_HOME": f"https://{domain}",
            "WP_SITE_TITLE": org_name,
        },
    )
    if fly_secrets_ticket:
        result.tickets.append(fly_secrets_ticket)
        file_ticket(sh, fly_secrets_ticket)
    else:
        result.steps_completed.append("set_fly_secrets")

    if image_ref:
        deploy_ticket = step_deploy_image(sh, fly_app=fly_app, image_ref=image_ref, region=inputs.region)
        if deploy_ticket:
            result.tickets.append(deploy_ticket)
            file_ticket(sh, deploy_ticket)
        else:
            result.steps_completed.append("deploy_image")

    if http and zone_lookup is not None:
        dns_ticket = step_ensure_dns_record(
            http, domain=domain, target=f"{fly_app}.fly.dev", zone_lookup=zone_lookup
        )
        if dns_ticket:
            result.tickets.append(dns_ticket)
            file_ticket(sh, dns_ticket)
        else:
            result.steps_completed.append("ensure_dns_record")

        cert_ticket = step_ensure_cert(sh, fly_app=fly_app, domain=domain)
        if cert_ticket:
            result.tickets.append(cert_ticket)
            file_ticket(sh, cert_ticket)
        else:
            result.steps_completed.append("ensure_cert")

    if fetch and image_ref:
        reached, elapsed = poll_until_200(fetch, f"https://{fly_app}.fly.dev/")
        result.steps_completed.append(f"poll_until_200({'ok' if reached else 'timed_out'},{elapsed:.1f}s)")
        if reached:
            result.preview_url = f"https://{fly_app}.fly.dev/"
        else:
            result.tickets.append(
                ProvisioningTicket(
                    title=f"{fly_app} never reached 200 within the poll window",
                    tried=f"poll https://{fly_app}.fly.dev/ with sub-second backoff up to 300s",
                    hit=f"still not 200 after {elapsed:.1f}s",
                    need="Investigate the Machine's boot log — this is a hard failure, not a missing credential.",
                )
            )

    return result


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: provision.py <domain> [org_name]", file=sys.stderr)
        return 2
    domain = argv[0]
    org_name = argv[1] if len(argv) > 1 else None
    inputs = ProvisionInputs(domain=domain, org_name=org_name)
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
