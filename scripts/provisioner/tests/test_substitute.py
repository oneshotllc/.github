from __future__ import annotations

from pathlib import Path

from provisioner.substitute import substitute, remaining_tokens


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".oneshot").mkdir(parents=True)
    (repo / "theme" / "oneshot-block-theme").mkdir(parents=True)
    (repo / ".oneshot" / "repo.yml").write_text(
        "org_slug: __ORG_SLUG__\norg_name: __ORG_NAME__\nfly_app: __FLY_APP__\ndomain: __DOMAIN__\ntheme_tokens: __THEME_TOKENS__\n"
    )
    (repo / "fly.toml").write_text('app = "__FLY_APP__"\nprimary_region = "__FLY_REGION__"\n')
    (repo / "theme" / "oneshot-block-theme" / "style.css").write_text(
        "/*\nTheme Name: __ORG_NAME__\nTheme URI: https://__DOMAIN__\n*/\n"
    )
    return repo


VALUES = {
    "__ORG_SLUG__": "acme-corp",
    "__ORG_NAME__": "Acme Corp",
    "__FLY_APP__": "acme-corp",
    "__FLY_REGION__": "ord",
    "__DOMAIN__": "acme-corp.oneshot.help",
    "__THEME_TOKENS__": "default",
}


def test_substitute_replaces_every_token(tmp_path: Path):
    repo = make_repo(tmp_path)
    changed = substitute(repo, VALUES)
    assert set(changed) == {".oneshot/repo.yml", "fly.toml", "theme/oneshot-block-theme/style.css"}
    assert remaining_tokens(repo) == {}
    assert "acme-corp" in (repo / "fly.toml").read_text()
    assert "__" not in (repo / ".oneshot" / "repo.yml").read_text().replace("__ORG_SLUG__", "")


def test_substitute_is_idempotent_second_run_changes_nothing(tmp_path: Path):
    repo = make_repo(tmp_path)
    substitute(repo, VALUES)
    changed_again = substitute(repo, VALUES)
    assert changed_again == []


def test_substitute_rejects_unknown_token(tmp_path: Path):
    repo = make_repo(tmp_path)
    try:
        substitute(repo, {"__NOT_A_REAL_TOKEN__": "x"})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_remaining_tokens_detects_leftover(tmp_path: Path):
    repo = make_repo(tmp_path)
    # Deliberately substitute only some tokens.
    partial = dict(VALUES)
    del partial["__DOMAIN__"]
    substitute(repo, partial)
    leftover = remaining_tokens(repo)
    assert "__DOMAIN__" in leftover
