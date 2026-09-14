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


def test_step_create_repo_converges_when_already_exists():
    sh = FakeShell({
        ("view", "existing/repo"): cp(returncode=0, stdout='{"name":"repo"}'),
        ("api",): cp(returncode=0, stdout='[{"name":"main"}]'),  # copy already landed
    })
    step_create_repo(sh, repo="existing/repo", template_repo="org/tmpl")
    assert not any("create" in c for c in sh.calls)


class BranchesAfterNFakeShell(FakeShell):
    """FakeShell whose `gh api .../branches` responds empty for the first
    `empty_polls` calls, then non-empty forever after — models GitHub's
    async template-copy job landing partway through a backoff schedule.
    """

    def __init__(self, empty_polls: int, responses=None):
        super().__init__(responses)
        self.empty_polls = empty_polls
        self.branch_calls = 0

    def _respond(self, argv):
        self.calls.append(argv)
        if "branches" in " ".join(argv):
            self.branch_calls += 1
            if self.branch_calls <= self.empty_polls:
                return cp(returncode=0, stdout="[]")
            return cp(returncode=0, stdout='[{"name":"main"}]')
        for pattern, resp in self.responses.items():
            if all(p in argv for p in pattern):
                return resp
        return self.default


def test_step_create_repo_backs_off_then_succeeds_once_copy_lands(monkeypatch):
    """GitHub's --template copy is async: gh repo create returns before the
    new repo has any commits, and the copy can legitimately take longer
    than a short fixed window (observed live: run 34791251371 raced an
    immediate clone into an empty repo; run 34791830298 then timed out on
    a single fixed 20s window even though the copy was still in progress).
    A template copy that looks empty for the first several polls and then
    succeeds must produce a normal, successful step_create_repo call — no
    exception, no ticket — once any poll in the backoff schedule sees
    branches.
    """
    sleeps: list[float] = []
    monkeypatch.setattr("provisioner.provision._time.sleep", sleeps.append)

    sh = BranchesAfterNFakeShell(
        empty_polls=4,  # succeeds on the 5th check, mid-schedule
        responses={("view", "org/new-repo"): cp(returncode=1)},
    )
    step_create_repo(sh, repo="org/new-repo", template_repo="org/tmpl")

    assert sh.branch_calls == 5
    # Slept between polls using the real (non-zero) backoff schedule —
    # proves this test exercises actual exponential backoff, not a stub.
    assert len(sleeps) == 4
    assert sleeps == [1, 2, 4, 8]


def test_step_create_repo_self_heals_by_deleting_and_recreating_stuck_name(monkeypatch):
    """Deleting then recreating a repo under the SAME name is a distinct,
    worse failure mode than a plain slow copy (observed live: run
    34792369730 — deleted then immediately recreated "template-proof",
    template-copy never landed a branch in a full 180s backoff, but an
    identical create succeeded in under 20s once given a cooldown gap).
    This is exactly Step 2 of the acceptance test (delete, then restore
    under the same org_name), so step_create_repo must self-heal it: if
    the first full backoff round never sees branches, delete the
    still-empty repo and try once more before giving up.
    """
    monkeypatch.setattr("provisioner.provision._time.sleep", lambda *_: None)
    state = {"branch_calls_this_round": 0, "creates": 0, "deletes": 0}

    class StuckThenHealsFakeShell(FakeShell):
        def _respond(self, argv):
            self.calls.append(argv)
            joined = " ".join(argv)
            if "create" in argv and "repo" in argv:
                state["creates"] += 1
                state["branch_calls_this_round"] = 0
                return cp(returncode=0)
            if "delete" in argv:
                state["deletes"] += 1
                return cp(returncode=0)
            if "branches" in joined:
                state["branch_calls_this_round"] += 1
                # First create's round: always empty. Second create's
                # round: succeeds immediately.
                if state["creates"] >= 2:
                    return cp(returncode=0, stdout='[{"name":"main"}]')
                return cp(returncode=0, stdout="[]")
            for pattern, resp in self.responses.items():
                if all(p in argv for p in pattern):
                    return resp
            return self.default

    sh = StuckThenHealsFakeShell({("view", "org/stuck-name"): cp(returncode=1)})
    step_create_repo(
        sh, repo="org/stuck-name", template_repo="org/tmpl",
        poll_schedule=(0, 0),
    )

    assert state["creates"] == 2, "expected exactly one self-heal recreate"
    assert state["deletes"] == 1, "expected exactly one delete before the recreate"


def test_step_create_repo_raises_after_self_heal_also_fails():
    """If even the delete+recreate self-heal never lands branches, this is
    a real, unrecoverable-for-now GitHub-side stall — still not a human
    decision, so it must raise (fail the run loudly), never a ticket.
    """
    sh = FakeShell({
        ("view", "org/stuck-repo"): cp(returncode=1),
        ("api",): cp(returncode=0, stdout="[]"),
    })
    with pytest.raises(TemplateCopyTimeout, match="org/stuck-repo"):
        step_create_repo(
            sh, repo="org/stuck-repo", template_repo="org/tmpl",
            poll_schedule=(0, 0),
        )
    delete_calls = [c for c in sh.calls if "delete" in c]
    assert len(delete_calls) == 1, "expected exactly one self-heal delete attempt"


def test_step_create_repo_raises_after_full_backoff_window_expires():
    """Timing/races/slow APIs are never a human decision — on full timeout
    this must raise (failing the run loudly), not return/file a ticket.
    """
    sh = FakeShell({
        ("view", "org/stuck-repo"): cp(returncode=1),
        ("api",): cp(returncode=0, stdout="[]"),
    })
    with pytest.raises(TemplateCopyTimeout, match="org/stuck-repo"):
        step_create_repo(
            sh, repo="org/stuck-repo", template_repo="org/tmpl",
            poll_schedule=(0, 0, 0),
        )


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
        ("api",): cp(returncode=0, stdout='[{"name":"main"}]'),  # copy already landed
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
    monkeypatch.setattr("provisioner.provision._time.sleep", lambda *_: None)
    orgs_path = tmp_path / "organizations.yaml"
    orgs_path.write_text(yaml.safe_dump([]))
    sh = FakeShell(responses={
        ("repo", "view"): cp(returncode=1),
        ("api",): cp(returncode=0, stdout='[{"name":"main"}]'),
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


def test_step_create_repo_waits_when_existing_repo_is_still_empty(monkeypatch):
    """Regression, live run 34791830298: an earlier run created the repo then
    died while GitHub's template copy was still in flight. step_create_repo
    returned early on "repo exists", so every retry stranded on a branchless
    repo and the provisioner ticketed a human instead of simply waiting."""
    monkeypatch.setattr("provisioner.provision._time.sleep", lambda *_: None)
    state = {"n": 0}

    class SlowCopyShell(FakeShell):
        def _respond(self, argv):
            self.calls.append(argv)
            if "branches" in " ".join(argv):
                state["n"] += 1
                return cp(returncode=0, stdout="[]" if state["n"] < 3 else '[{"name":"main"}]')
            for pattern, resp in self.responses.items():
                if all(p in argv for p in pattern):
                    return resp
            return self.default

    sh = SlowCopyShell({("view", "org/empty-repo"): cp(returncode=0, stdout='{"name":"empty-repo"}')})
    step_create_repo(sh, repo="org/empty-repo", template_repo="org/tmpl")
    assert state["n"] == 3, "must keep polling an existing-but-empty repo"
    assert not any("create" in c for c in sh.calls), "must not recreate an existing repo"
