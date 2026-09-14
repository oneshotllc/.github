"""Unit tests for the provisioner's pure logic and step sequencing. Every
gh/flyctl call is faked via FakeShell so these run with no network and no
real GitHub/Fly state — see BRIEF step 2's "tested" requirement.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from provisioner.provision import (
    ProvisionInputs,
    ProvisioningTicket,
    TemplateCopyTimeout,
    derive_slug,
    generate_passphrase,
    provision_site,
    TemplateCopyTimeout,
    step_create_repo,
    step_ensure_fly_app,
    step_register_organization,
    step_set_repo_secrets,
)


def cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeShell:
    """Records every call; scripted per-call responses keyed by a matcher
    function so a test can make "repo view" fail (not-yet-created) while
    "repo create" succeeds, etc.
    """

    def __init__(self, responses: dict[tuple, subprocess.CompletedProcess] | None = None):
        self.calls: list[list[str]] = []
        self.responses = responses or {}
        self.default = cp(returncode=0)

    def _respond(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        key = tuple(argv)
        for pattern, resp in self.responses.items():
            if all(p in argv for p in pattern):
                return resp
        return self.default

    def run(self, argv, check: bool = False):
        return self._respond(argv)

    def gh(self, *args, check: bool = False):
        return self._respond(["gh", *args])

    def git(self, *args, cwd=None, check: bool = False):
        return self._respond(["git", *args])


# --- derive_slug -------------------------------------------------------


@pytest.mark.parametrize(
    "org_name,expected",
    [
        ("Voices of Power", "voices-of-power"),
        ("Ben's site (10,000 Lakes)", "ben-s-site-10-000-lakes"),
        ("  Good   Neighbor  ", "good-neighbor"),
        ("OneShot!!!", "oneshot"),
        ("café-société", "caf-soci-t"),
    ],
)
def test_derive_slug(org_name, expected):
    assert derive_slug(org_name) == expected


def test_derive_slug_rejects_no_alphanumeric_content():
    with pytest.raises(ValueError):
        derive_slug("!!!")


def test_derive_slug_is_idempotent_on_its_own_output():
    slug = derive_slug("Voices of Power")
    assert derive_slug(slug) == slug


# --- generate_passphrase ------------------------------------------------


def test_generate_passphrase_shape():
    p = generate_passphrase()
    parts = p.split("-")
    assert len(parts) == 5  # 4 words + 2-digit suffix
    assert parts[-1].isdigit() and len(parts[-1]) == 2
    for word in parts[:-1]:
        assert word.isalpha()


def test_generate_passphrase_is_not_a_hex_token():
    p = generate_passphrase()
    assert not all(c in "0123456789abcdef" for c in p.replace("-", ""))


def test_generate_passphrase_varies_across_calls():
    seen = {generate_passphrase() for _ in range(20)}
    assert len(seen) > 1


# --- step_create_repo -----------------------------------------------------


def _content(*names):
    return cp(returncode=0, stdout="[" + ",".join(f'{{"name":"{n}"}}' for n in names) + "]")


_EMPTY_REPO = cp(returncode=0, stdout='{"message":"This repository is empty.","status":"404"}')


def test_step_create_repo_converges_when_already_seeded():
    """Existing repo WITH content: no create, no seed, no work."""
    sh = FakeShell({
        ("view", "existing/repo"): cp(returncode=0, stdout='{"name":"repo"}'),
        ("api",): _content("README.md"),
    })
    step_create_repo(sh, repo="existing/repo", template_repo="org/tmpl")
    assert not any("create" in c for c in sh.calls)
    assert not any("clone" in c for c in sh.calls)


def test_step_create_repo_seeds_a_new_repo_from_the_template():
    """New repo: create empty, then seed by clone+push. The unreliable
    --template flag must NOT be used (live runs 34791830298 / 34792691772
    both produced a branch ref with zero content)."""
    state = {"n": 0}

    class SeedingShell(FakeShell):
        def _respond(self, argv):
            self.calls.append(argv)
            joined = " ".join(argv)
            if "contents" in joined:
                state["n"] += 1
                return _EMPTY_REPO if state["n"] == 1 else _content("README.md")
            for pattern, resp in self.responses.items():
                if all(p in argv for p in pattern):
                    return resp
            return self.default

    sh = SeedingShell({("view", "org/new-repo"): cp(returncode=1)})
    step_create_repo(sh, repo="org/new-repo", template_repo="org/tmpl")
    flat = [" ".join(c) for c in sh.calls]
    assert any("repo create" in f or ("create" in f and "org/new-repo" in f) for f in flat)
    assert not any("--template" in f for f in flat), "must not rely on GitHub's async template copy"
    assert any("clone" in f for f in flat) and any("push" in f for f in flat)


def test_step_create_repo_seeds_an_existing_but_empty_repo():
    """Regression, live run 34791830298: an earlier run created the repo and
    died before content landed. Returning early on 'repo exists' stranded
    every retry on a contentless repo and produced a bogus human ticket."""
    state = {"n": 0}

    class SeedingShell(FakeShell):
        def _respond(self, argv):
            self.calls.append(argv)
            if "contents" in " ".join(argv):
                state["n"] += 1
                return _EMPTY_REPO if state["n"] == 1 else _content("README.md")
            for pattern, resp in self.responses.items():
                if all(p in argv for p in pattern):
                    return resp
            return self.default

    sh = SeedingShell({("view", "org/empty-repo"): cp(returncode=0, stdout='{"name":"empty-repo"}')})
    step_create_repo(sh, repo="org/empty-repo", template_repo="org/tmpl")
    flat = [" ".join(c) for c in sh.calls]
    assert not any("repo create" in f for f in flat), "must not recreate an existing repo"
    assert any("push" in f for f in flat), "must seed the empty repo"


def test_step_create_repo_raises_when_seeding_produces_nothing():
    """A repo still empty after seeding is a loud RUN failure, never a
    ticket: timing and API problems are code, not human decisions."""
    sh = FakeShell({
        ("view", "org/stuck-repo"): cp(returncode=1),
        ("api",): _EMPTY_REPO,
    })
    with pytest.raises(TemplateCopyTimeout):
        step_create_repo(sh, repo="org/stuck-repo", template_repo="org/tmpl")


# --- step_ensure_fly_app -------------------------------------------------


def test_ensure_fly_app_creates_when_absent():
    sh = FakeShell(responses={
        ("apps", "list"): cp(returncode=0, stdout="[]"),
    })
    ticket = step_ensure_fly_app(sh, fly_app="acme", region="ord")
    assert ticket is None
    create_calls = [c for c in sh.calls if "create" in c and "apps" in c]
    assert create_calls, "expected an apps create call"


def test_ensure_fly_app_converges_when_already_exists():
    sh = FakeShell(responses={
        ("apps", "list"): cp(returncode=0, stdout='[{"Name": "acme"}]'),
    })
    ticket = step_ensure_fly_app(sh, fly_app="acme", region="ord")
    assert ticket is None
    create_calls = [c for c in sh.calls if "create" in c and "apps" in c]
    assert not create_calls, "must not attempt to create an app that already exists"

def test_ensure_fly_app_creates_exactly_one_volume_when_absent():
    sh = FakeShell(responses={
        ("apps", "list"): cp(returncode=0, stdout="[]"),
        ("volumes", "list"): cp(returncode=0, stdout="[]"),
    })
    step_ensure_fly_app(sh, fly_app="acme", region="ord")
    volume_creates = [c for c in sh.calls if "volumes" in c and "create" in c]
    assert len(volume_creates) == 1


def test_ensure_fly_app_does_not_create_a_second_volume_when_one_exists():
    """Live bug: template-proof accumulated THREE wp_uploads volumes because
    nothing checked for an existing one before calling `flyctl volumes
    create`. Running provisioning N times against an app that already has
    the volume must create zero more.
    """
    sh = FakeShell(responses={
        ("apps", "list"): cp(returncode=0, stdout='[{"Name": "acme"}]'),
        ("volumes", "list"): cp(returncode=0, stdout='[{"Name": "wp_uploads", "id": "vol_existing"}]'),
    })
    step_ensure_fly_app(sh, fly_app="acme", region="ord")
    volume_creates = [c for c in sh.calls if "volumes" in c and "create" in c]
    assert volume_creates == [], "must not create a second wp_uploads volume when one already exists"


def test_ensure_fly_app_run_three_times_yields_exactly_one_volume_create():
    """Simulates calling step_ensure_fly_app three times in a row (as three
    provisioning runs would): the first call sees no volume and creates one;
    the fake then reports that volume as existing for every subsequent call,
    matching real `flyctl volumes list` behavior. Exactly one create total.
    """
    state = {"has_volume": False}

    class StatefulShell(FakeShell):
        def run(self, argv, check: bool = False):
            if "volumes" in argv and "list" in argv:
                stdout = '[{"Name": "wp_uploads", "id": "vol_existing"}]' if state["has_volume"] else "[]"
                self.calls.append(argv)
                return cp(returncode=0, stdout=stdout)
            if "volumes" in argv and "create" in argv:
                state["has_volume"] = True
            return super().run(argv, check=check)

    sh = StatefulShell(responses={
        ("apps", "list"): cp(returncode=0, stdout='[{"Name": "acme"}]'),
    })
    for _ in range(3):
        step_ensure_fly_app(sh, fly_app="acme", region="ord")
    volume_creates = [c for c in sh.calls if "volumes" in c and "create" in c]
    assert len(volume_creates) == 1, f"expected exactly one volume create across 3 runs, got {len(volume_creates)}"


# --- step_register_organization -----------------------------------------


def test_register_organization_appends_new_entry(tmp_path: Path):
    orgs_path = tmp_path / "organizations.yaml"
    orgs_path.write_text(yaml.safe_dump([{"id": "oneshot", "name": "OneShot", "repos": [], "contacts": []}]))
    step_register_organization(organizations_path=orgs_path, org_id="acme", org_name="Acme", repo="oneshotmn/acme")
    data = yaml.safe_load(orgs_path.read_text())
    assert any(e["id"] == "acme" and e["repos"] == ["oneshotmn/acme"] for e in data)
    assert any(e["id"] == "oneshot" for e in data)  # untouched


def test_register_organization_is_idempotent(tmp_path: Path):
    orgs_path = tmp_path / "organizations.yaml"
    orgs_path.write_text(yaml.safe_dump([]))
    for _ in range(3):
        step_register_organization(organizations_path=orgs_path, org_id="acme", org_name="Acme", repo="oneshotmn/acme")
    data = yaml.safe_load(orgs_path.read_text())
    matching = [e for e in data if e["id"] == "acme"]
    assert len(matching) == 1
    assert matching[0]["repos"] == ["oneshotmn/acme"]  # not duplicated


def test_register_organization_preserves_existing_repos(tmp_path: Path):
    orgs_path = tmp_path / "organizations.yaml"
    orgs_path.write_text(yaml.safe_dump([{"id": "acme", "name": "Acme", "repos": ["oneshotmn/other"], "contacts": []}]))
    step_register_organization(organizations_path=orgs_path, org_id="acme", org_name="Acme", repo="oneshotmn/acme")
    data = yaml.safe_load(orgs_path.read_text())
    entry = next(e for e in data if e["id"] == "acme")
    assert set(entry["repos"]) == {"oneshotmn/other", "oneshotmn/acme"}


# --- step_set_repo_secrets ------------------------------------------------

def test_set_repo_secrets_succeeds_when_source_exists_and_gh_succeeds(tmp_path: Path, monkeypatch):
    secret_file = tmp_path / "secret"
    secret_file.write_text("shh")

    class FakeCompleted:
        returncode = 0

    monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeCompleted())
    sh = FakeShell()
    ticket = step_set_repo_secrets(sh, repo="oneshotmn/acme", secret_names_and_paths={"X": str(secret_file)})
    assert ticket is None


# --- provision_site end-to-end (fully faked shell) ------------------------


def test_provision_site_full_run_reports_every_step(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("provisioner.provision._time.sleep", lambda *_: None)
    orgs_path = tmp_path / "organizations.yaml"
    orgs_path.write_text(yaml.safe_dump([]))
    sh = FakeShell(responses={
        ("repo", "view"): cp(returncode=1),  # does not exist yet
        ("api",): cp(returncode=0, stdout='[{"name":"main"}]'),  # branches after template copy
        ("apps", "list"): cp(returncode=0, stdout="[]"),
        ("workflow", "run"): cp(returncode=0),
    })

    class FakeCompleted:
        returncode = 0
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeCompleted())

    result = provision_site(
        ProvisionInputs(domain="acme-corp.oneshot.help", org_name="Acme Corp"), sh,
        organizations_path=orgs_path, secret_sources={}, skip_deploy=True,
    )

    assert result.slug == "acme-corp"
    assert result.repo == "oneshotmn/acme-corp"
    assert result.fly_app == "acme-corp"
    assert "create_repo" in result.steps_completed
    assert "ensure_fly_app" in result.steps_completed
    assert "register_organization" in result.steps_completed
    assert result.tickets == []


def test_provision_site_is_idempotent_when_repo_already_exists(tmp_path: Path, monkeypatch):
    orgs_path = tmp_path / "organizations.yaml"
    orgs_path.write_text(yaml.safe_dump([]))
    sh = FakeShell(responses={
        ("repo", "view"): cp(returncode=0),  # already exists
        ("api",): cp(returncode=0, stdout='[{"name":"main"}]'),  # copy already landed
        ("apps", "list"): cp(returncode=0, stdout='[{"Name": "acme-corp-oneshot-help"}]'),
        ("workflow", "run"): cp(returncode=0),
    })

    class FakeCompleted:
        returncode = 0
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeCompleted())

    result = provision_site(
        ProvisionInputs(domain="acme-corp.oneshot.help", org_name="Acme Corp"), sh,
        organizations_path=orgs_path, secret_sources={}, skip_deploy=True,
    )

    repo_create_calls = [c for c in sh.calls if "repo" in c and "create" in c]
    fly_create_calls = [c for c in sh.calls if "apps" in c and "create" in c]
    assert not repo_create_calls, "must not attempt to recreate an existing repo"
    assert not fly_create_calls, "must not attempt to recreate an existing Fly app"
    assert "create_repo" in result.steps_completed  # step still reports converged

def test_cishell_exposes_git_for_seeding():
    """Regression, live run 34793088841: _seed_from_template called sh.git()
    but CIShell only had run/gh, so every real provision died with
    AttributeError. The CI shell must satisfy the same interface the steps
    use, and must never put the token in a URL (it would land in logs)."""
    import inspect
    from provisioner.ci_entrypoint import CIShell

    assert hasattr(CIShell, "git"), "CIShell must implement git() like Shell"
    src = inspect.getsource(CIShell.git)
    assert "x-access-token:" not in src.split("basic")[0].split("\n")[-1] or "extraheader" in src
    assert "extraheader" in src, "token must be passed as a header, never embedded in a URL"


def test_app_token_is_minted_with_org_scope():
    """Regression, live run 34793604383: create-github-app-token without an
    `owner` mints a token scoped to the CALLING repo only, so the push that
    seeds a freshly created repo fails with 'Repository not found'. The
    provisioner creates repos, so its token must be org-scoped."""
    from pathlib import Path
    wf = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "provision-site.yml"
    text = wf.read_text()
    mint = text.split("create-github-app-token")[1].split("- name:")[0]
    assert "owner:" in mint, "app token must be minted with owner: for org-wide scope"


def test_deploy_does_not_pass_region_flag():
    """Regression, live run 34798261207: step_deploy_image passed --region to
    `flyctl deploy`, which has no such flag. It died with 'unknown flag:
    --region' and the provisioner mis-reported it as a missing Fly token."""
    import inspect
    from provisioner.provision import step_deploy_image
    src = inspect.getsource(step_deploy_image)
    assert '"--region"' not in src, "flyctl deploy has no --region flag"


def test_deploy_failure_raises_instead_of_ticketing():
    """A failed deploy is code (wrong command, image or app), never a human
    decision. Only a credential that cannot exist here may become a ticket."""
    from provisioner.provision import ProvisioningError, step_deploy_image

    class FailingShell(FakeShell):
        def run(self, argv, check=False):
            self.calls.append(argv)
            return cp(returncode=1, stderr="Error: unknown flag: --region")

    with pytest.raises(ProvisioningError):
        step_deploy_image(FailingShell({}), fly_app="app", image_ref="img:live", region="ord")


def test_infra_steps_raise_instead_of_ticketing():
    """Brian's rule: only a credential that cannot exist on this host may
    become a ticket. Infrastructure failures - app create, secrets, DNS -
    are code or config, so they must fail the RUN loudly.

    Live examples that wrongly reached Brian: #13/#14/#15 (a nonexistent
    --region flag reported as a missing Fly token) and #16 (an org-scoped
    token I had swapped in, reported as missing org access)."""
    import inspect
    from provisioner import provision

    for name in ("step_ensure_fly_app", "step_set_fly_secrets",
                 "step_ensure_dns_record", "step_set_repo_secrets"):
        src = inspect.getsource(getattr(provision, name))
        assert "return ProvisioningTicket(" not in src, (
            f"{name} must raise ProvisioningError, not file a ticket"
        )


def test_fly_app_name_derives_from_whole_domain():
    """Regression, live run 34800109357: fly_app was the bare slug, so
    oneshot.help asked for the Fly app `oneshot` - a name already held by a
    stranger (Fly names are globally unique). Creation failed with "taken",
    the step treated that as success, and the run then died on `secrets
    import` with a bare "unauthorized" against an app we do not own."""
    from provisioner.provision import derive_fly_app
    assert derive_fly_app("oneshot.help") == "oneshot-help"
    assert derive_fly_app("voices-of-power.org") == "voices-of-power-org"


def test_taken_app_name_is_not_treated_as_success():
    """A globally-taken name must fail the run loudly, not silently proceed
    against someone else's app."""
    from provisioner.provision import ProvisioningError, step_ensure_fly_app

    class TakenShell(FakeShell):
        def run(self, argv, check=False):
            self.calls.append(argv)
            if "list" in argv:
                return cp(returncode=0, stdout="[]")
            return cp(returncode=1, stderr="App names are unique across all of Fly.io ... this name may be held")

    with pytest.raises(ProvisioningError, match="taken"):
        step_ensure_fly_app(TakenShell({}), fly_app="oneshot", region="ord")
