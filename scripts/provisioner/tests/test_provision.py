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
    derive_slug,
    generate_passphrase,
    provision_site,
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


def test_ensure_fly_app_files_ticket_on_real_failure():
    sh = FakeShell(responses={
        ("apps", "list"): cp(returncode=0, stdout="[]"),
        ("apps", "create"): cp(returncode=1, stderr="permission denied"),
    })
    ticket = step_ensure_fly_app(sh, fly_app="acme", region="ord")
    assert isinstance(ticket, ProvisioningTicket)
    assert "permission denied" in ticket.hit
    assert ticket.assignee == "bwoestman"


def test_ensure_fly_app_treats_already_taken_as_converged_not_a_ticket():
    sh = FakeShell(responses={
        ("apps", "list"): cp(returncode=0, stdout="[]"),
        ("apps", "create"): cp(returncode=1, stderr="Name has already been taken"),
    })
    ticket = step_ensure_fly_app(sh, fly_app="acme", region="ord")
    assert ticket is None


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


def test_set_repo_secrets_files_ticket_when_source_missing(tmp_path: Path):
    sh = FakeShell()
    missing_path = tmp_path / "does-not-exist"
    ticket = step_set_repo_secrets(sh, repo="oneshotmn/acme", secret_names_and_paths={"X": str(missing_path)})
    assert isinstance(ticket, ProvisioningTicket)
    assert "X" in ticket.title


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
    orgs_path = tmp_path / "organizations.yaml"
    orgs_path.write_text(yaml.safe_dump([]))
    sh = FakeShell(responses={
        ("repo", "view"): cp(returncode=1),  # does not exist yet
        ("apps", "list"): cp(returncode=0, stdout="[]"),
        ("workflow", "run"): cp(returncode=0),
    })

    class FakeCompleted:
        returncode = 0
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeCompleted())

    result = provision_site(
        ProvisionInputs(org_name="Acme Corp"), sh, organizations_path=orgs_path, secret_sources={}
    )

    assert result.slug == "acme-corp"
    assert result.repo == "oneshotmn/acme-corp"
    assert result.fly_app == "acme-corp"
    assert "create_repo" in result.steps_completed
    assert "ensure_fly_app" in result.steps_completed
    assert "register_organization" in result.steps_completed
    assert "trigger_preview" in result.steps_completed
    assert result.tickets == []


def test_provision_site_is_idempotent_when_repo_already_exists(tmp_path: Path, monkeypatch):
    orgs_path = tmp_path / "organizations.yaml"
    orgs_path.write_text(yaml.safe_dump([]))
    sh = FakeShell(responses={
        ("repo", "view"): cp(returncode=0),  # already exists
        ("apps", "list"): cp(returncode=0, stdout='[{"Name": "acme-corp"}]'),
        ("workflow", "run"): cp(returncode=0),
    })

    class FakeCompleted:
        returncode = 0
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeCompleted())

    result = provision_site(
        ProvisionInputs(org_name="Acme Corp"), sh, organizations_path=orgs_path, secret_sources={}
    )

    repo_create_calls = [c for c in sh.calls if "repo" in c and "create" in c]
    fly_create_calls = [c for c in sh.calls if "apps" in c and "create" in c]
    assert not repo_create_calls, "must not attempt to recreate an existing repo"
    assert not fly_create_calls, "must not attempt to recreate an existing Fly app"
    assert "create_repo" in result.steps_completed  # step still reports converged


def test_provision_site_files_ticket_and_continues_when_fly_blocked(tmp_path: Path, monkeypatch):
    orgs_path = tmp_path / "organizations.yaml"
    orgs_path.write_text(yaml.safe_dump([]))
    sh = FakeShell(responses={
        ("repo", "view"): cp(returncode=1),
        ("apps", "list"): cp(returncode=0, stdout="[]"),
        ("apps", "create"): cp(returncode=1, stderr="permission denied"),
        ("workflow", "run"): cp(returncode=0),
    })

    class FakeCompleted:
        returncode = 0
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeCompleted())

    result = provision_site(
        ProvisionInputs(org_name="Acme Corp"), sh, organizations_path=orgs_path, secret_sources={}
    )

    assert any("Fly app" in t.title for t in result.tickets)
    # Provisioning must continue past the blocked step:
    assert "register_organization" in result.steps_completed
    assert "trigger_preview" in result.steps_completed
