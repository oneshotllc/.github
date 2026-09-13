#!/usr/bin/env python3
"""Replace site-template's __TOKEN__ placeholders with real values in a
checked-out copy of a provisioned repo. Idempotent: running it twice with
the same inputs produces the same file contents (no-op diff, no duplicate
commit) because it substitutes by exact token match, not by appending.

Used by both provision-site.yml (CI) and a human re-running provisioning
locally — there is exactly one substitution implementation.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Every file that ships a literal __TOKEN__ in site-template. Adding a new
# templated file means adding it here — grep for "__" first to check.
TEMPLATED_FILES = (
    ".oneshot/repo.yml",
    "fly.toml",
    "theme/oneshot-block-theme/style.css",
)

TOKENS = (
    "__ORG_SLUG__",
    "__ORG_NAME__",
    "__FLY_APP__",
    "__FLY_REGION__",
    "__DOMAIN__",
    "__THEME_TOKENS__",
)


def substitute(repo_dir: Path, values: dict[str, str]) -> list[str]:
    """Returns the list of files actually changed (empty on a re-run with
    identical values — that is the idempotence contract).
    """
    unknown = set(values) - set(TOKENS)
    if unknown:
        raise ValueError(f"unknown token(s) passed to substitute(): {sorted(unknown)}")

    changed: list[str] = []
    for rel_path in TEMPLATED_FILES:
        path = repo_dir / rel_path
        if not path.exists():
            continue
        original = path.read_text(encoding="utf-8")
        updated = original
        for token, value in values.items():
            updated = updated.replace(token, value)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed.append(rel_path)
    return changed


def remaining_tokens(repo_dir: Path) -> dict[str, list[str]]:
    """After substitution, which __TOKEN__ placeholders are still present
    and in which files — used as a hard gate: provisioning must not report
    success while a template token survives unsubstituted.
    """
    found: dict[str, list[str]] = {}
    for rel_path in TEMPLATED_FILES:
        path = repo_dir / rel_path
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for token in TOKENS:
            if token in text:
                found.setdefault(token, []).append(rel_path)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_dir", type=Path)
    parser.add_argument("--org-slug", required=True)
    parser.add_argument("--org-name", required=True)
    parser.add_argument("--fly-app", required=True)
    parser.add_argument("--fly-region", default="ord")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--theme-tokens", default="default")
    args = parser.parse_args(argv)

    values = {
        "__ORG_SLUG__": args.org_slug,
        "__ORG_NAME__": args.org_name,
        "__FLY_APP__": args.fly_app,
        "__FLY_REGION__": args.fly_region,
        "__DOMAIN__": args.domain,
        "__THEME_TOKENS__": args.theme_tokens,
    }
    changed = substitute(args.repo_dir, values)
    leftover = remaining_tokens(args.repo_dir)
    if leftover:
        print(f"substitute.py: unsubstituted tokens remain: {leftover}", file=sys.stderr)
        return 1
    print(f"substitute.py: changed {changed or '(nothing — already up to date)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
