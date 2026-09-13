# oneshotmn/.github

Org-wide GitHub defaults for the `oneshotmn` organization.

## What lives here

- `.github/ISSUE_TEMPLATE/bdd-ticket.yml` — the org's Issue Form. GitHub
  inherits this into every repo in the org that doesn't define its own
  `ISSUE_TEMPLATE`, current and future.
- `.github/ISSUE_TEMPLATE/config.yml` — `blank_issues_enabled: false`, so a
  blank issue can't skip the form.
- `.github/workflows/intake-gate-reusable.yml` — reusable workflow
  (`workflow_call`) that deterministically validates a new/edited issue
  against the form's rendered shape and labels it `intake:valid` or
  `invalid:shape`. No LLM calls.
- `.github/workflows/comment-gate-reusable.yml` — reusable workflow
  (`workflow_call`) that recognizes a small set of state-driving comment
  shapes (`answer:`, `approved`, `rejected: <reason>`, `/pull-ticket <n>`)
  and applies the corresponding label transition. No LLM calls.

Actions workflows do **not** inherit from this repo the way issue templates
do — each ticket-bearing repo carries a ~6-line caller stub that does
`uses: oneshotmn/.github/.github/workflows/intake-gate-reusable.yml@main`
(and the comment-gate equivalent) on its own `issues`/`issue_comment` triggers.

See the plan this implements:
`oneshotmn/oneshotmn` repo, `.hermes/plans/2026-09-12_deterministic-ticket-intake.md`
(local planning doc, not committed here).
