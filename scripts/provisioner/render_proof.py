#!/usr/bin/env python3
"""Render proof: the only accepted evidence that a provisioned site is live.

    python render_proof.py <url> <outdir>

Loads <url> in headless Chromium the way an anonymous visitor would (fresh
context, no cookies/storage, NO resolver or host overrides, no networking
flags) at two sizes -- a phone ("iPhone 13" descriptor) and a 1366x900
desktop -- and writes mobile.png, desktop.png and report.json to <outdir>.
Screenshots are taken whatever the verdict: a screenshot of an error page is
evidence too.

Why this exists: an HTTP 200 on the page HTML proved nothing. A provisioned
site answered 200 while its CSS/JS pointed at a domain with no DNS, so it
rendered broken on a real phone. This script runs on a GitHub-hosted runner
(clean network, outside our infrastructure) and its artifact is attached to
the CI run; nothing on the local Mac is part of the evidence chain.

Verdict rules live in judge() (pure, unit-tested). A render FAILS if:
  * navigation threw (DNS failure, connection closed, timeout, TLS error);
  * the main document status is not 200;
  * visible body text (document.body.innerText, trimmed) is < 20 chars;
  * any stylesheet or script request failed (network error or HTTP >= 400),
    from ANY host -- the incident was CSS/JS on a dead third-party domain --
    EXCEPT hosts on ANALYTICS_HOSTS below (analytics outages or blockers
    must not fail a deploy);
  * any same-site document/stylesheet/script/image request failed.
    "Same-site" = the request host equals the page host, or one is a
    subdomain of the other (no public-suffix list; deliberately simple).
Other third-party failures (fonts, xhr/fetch, beacons, images) are recorded
in report.json but do not fail the verdict. Exit 0 on PASS, 1 on FAIL.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

MIN_TEXT_LENGTH = 20
NAV_TIMEOUT_MS = 30_000
NETWORKIDLE_TIMEOUT_MS = 5_000
SCREENSHOT_TIMEOUT_MS = 15_000

# Failures of any resource type always count when same-site.
SAME_SITE_CRITICAL_TYPES = frozenset({"document", "stylesheet", "script", "image"})
# Failures of these types count from any host (minus analytics).
ANY_SITE_CRITICAL_TYPES = frozenset({"stylesheet", "script"})
# Third-party analytics/tag hosts whose failures never fail a render. Matched
# as the host itself or any subdomain of it. Keep this list short and obvious.
ANALYTICS_HOSTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "plausible.io",
    "cloudflareinsights.com",
    "segment.com",
    "segment.io",
    "hotjar.com",
    "clarity.ms",
    "facebook.net",
)

RENDERS = ("mobile", "desktop")


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def is_same_site(request_url: str, page_url: str) -> bool:
    req, page = _host(request_url), _host(page_url)
    if not req or not page:
        return False
    return _host_matches(req, page) or _host_matches(page, req)


def is_analytics(request_url: str) -> bool:
    host = _host(request_url)
    return any(_host_matches(host, d) for d in ANALYTICS_HOSTS)


def _describe_failure(f: dict) -> str:
    what = f.get("error") or f"HTTP {f.get('status')}"
    return f"{f.get('resource_type', '?')} {f.get('url', '?')} -> {what}"


def judge(report: dict) -> tuple[bool, list[str]]:
    """PASS/FAIL for a render report. Pure: no I/O, no network.

    `report` is the report.json shape: {"url": ..., "renders": [ {name,
    navigation_error, status, final_url, text_length, failed_requests:
    [{url, resource_type, error|status}], ...}, ... ]}. Every reason is
    prefixed with the render name so the summary says which view broke.
    """
    reasons: list[str] = []
    renders = report.get("renders") or []
    if report.get("error"):
        reasons.append(f"render_proof error: {report['error']}")
    if not renders:
        reasons.append("no renders were recorded")

    for r in renders:
        name = r.get("name", "?")
        page_url = r.get("final_url") or report.get("url", "")

        if r.get("navigation_error"):
            reasons.append(f"{name}: navigation failed: {r['navigation_error']}")
        status = r.get("status")
        if status != 200:
            reasons.append(f"{name}: main document status {status} (need 200)")
        text_length = r.get("text_length") or 0
        if text_length < MIN_TEXT_LENGTH:
            reasons.append(f"{name}: visible text length {text_length} < {MIN_TEXT_LENGTH}")

        for f in r.get("failed_requests") or []:
            rtype = f.get("resource_type", "")
            url = f.get("url", "")
            if rtype in SAME_SITE_CRITICAL_TYPES and is_same_site(url, page_url):
                reasons.append(f"{name}: same-site {_describe_failure(f)}")
            elif rtype in ANY_SITE_CRITICAL_TYPES and not is_analytics(url):
                reasons.append(f"{name}: {_describe_failure(f)}")

    return (not reasons), reasons


def _render(browser, playwright, name: str, url: str, outdir: Path) -> dict:
    if name == "mobile":
        device = dict(playwright.devices["iPhone 13"])
        device.pop("default_browser_type", None)
        context_args = device
    else:
        context_args = {"viewport": {"width": 1366, "height": 900}}

    result: dict = {
        "name": name,
        "context": {k: v for k, v in context_args.items()},
        "navigation_error": None,
        "status": None,
        "final_url": None,
        "title": None,
        "text_length": 0,
        "networkidle_reached": False,
        "failed_requests": [],
        "console_errors": [],
        "screenshot": f"{name}.png",
        "screenshot_error": None,
    }

    # Fresh context per render = anonymous first-time visitor. HTTPS errors
    # are NOT ignored: a bad certificate is a broken site.
    context = browser.new_context(**context_args)
    page = context.new_page()
    main_frame = page.main_frame

    def is_main_navigation(request) -> bool:
        try:
            return request.is_navigation_request() and request.frame == main_frame
        except Exception:
            return False

    def on_request_failed(request):
        if is_main_navigation(request):
            return  # reported as navigation_error
        result["failed_requests"].append({
            "url": request.url,
            "resource_type": request.resource_type,
            "error": request.failure or "request failed",
        })

    def on_response(response):
        if response.status < 400 or is_main_navigation(response.request):
            return  # main document status is reported separately
        result["failed_requests"].append({
            "url": response.url,
            "resource_type": response.request.resource_type,
            "status": response.status,
        })

    def on_console(msg):
        if msg.type == "error":
            result["console_errors"].append(msg.text)

    page.on("requestfailed", on_request_failed)
    page.on("response", on_response)
    page.on("console", on_console)
    page.on("pageerror", lambda exc: result["console_errors"].append(f"pageerror: {exc}"))

    try:
        try:
            response = page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
            result["status"] = response.status if response else None
        except Exception as exc:  # DNS, connection, TLS, timeout
            result["navigation_error"] = str(exc).strip().splitlines()[0]

        if not result["navigation_error"]:
            try:
                page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
                result["networkidle_reached"] = True
            except Exception:
                pass  # bounded: a chatty page is not a failure by itself

        result["final_url"] = page.url
        try:
            result["title"] = page.title()
            result["text_length"] = page.evaluate(
                "() => document.body ? document.body.innerText.trim().length : 0"
            )
        except Exception as exc:
            result["console_errors"].append(f"could not read page: {exc}")

        # Always screenshot, even on failure: an error page is evidence.
        try:
            page.screenshot(path=str(outdir / f"{name}.png"), full_page=True, timeout=SCREENSHOT_TIMEOUT_MS)
        except Exception as exc:
            result["screenshot_error"] = str(exc).strip().splitlines()[0]
    finally:
        context.close()
    return result


def render(url: str, outdir: Path) -> dict:
    from playwright.sync_api import sync_playwright  # imported lazily: judge() needs no browser

    outdir.mkdir(parents=True, exist_ok=True)
    report: dict = {
        "url": url,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runner": {
            "github_run": os.environ.get("GITHUB_RUN_ID"),
            "runner_os": os.environ.get("RUNNER_OS"),
        },
        "renders": [],
    }
    with sync_playwright() as p:
        # No args, no proxy, no --host-resolver-rules: the system resolver
        # of the machine running this is part of what is being proven.
        browser = p.chromium.launch(headless=True)
        report["browser_version"] = browser.version
        try:
            for name in RENDERS:
                report["renders"].append(_render(browser, p, name, url, outdir))
        finally:
            browser.close()
    return report


def _summary_markdown(url: str, ok: bool, reasons: list[str], report: dict) -> str:
    lines = [
        f"## Render proof: {'PASS' if ok else 'FAIL'}",
        "",
        f"- URL: `{url}`",
    ]
    for r in report.get("renders") or []:
        lines.append(
            f"- {r.get('name')}: status `{r.get('status')}`, final URL `{r.get('final_url')}`, "
            f"title `{r.get('title')}`, text length {r.get('text_length')}, "
            f"failed requests {len(r.get('failed_requests') or [])}"
        )
    if reasons:
        lines += ["", "**Reasons:**", ""] + [f"- {reason}" for reason in reasons]
    lines += [
        "",
        "Screenshots (`mobile.png`, `desktop.png`) and `report.json` are in this run's "
        "`render-proof-*` artifact. This render on a GitHub-hosted runner is the only "
        "accepted evidence the site is deployed.",
        "",
    ]
    return "\n".join(lines)


def _append_env_file(var: str, text: str) -> None:
    path = os.environ.get(var)
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: render_proof.py <url> <outdir>", file=sys.stderr)
        return 2
    url, outdir = argv[1].strip(), Path(argv[2])
    if urlsplit(url).scheme not in ("http", "https") or not _host(url):
        print(f"render proof FAIL: not an http(s) URL: {url!r}", file=sys.stderr)
        _append_env_file("GITHUB_OUTPUT", "verdict=FAIL\n")
        return 1
    outdir.mkdir(parents=True, exist_ok=True)

    try:
        report = render(url, outdir)
    except Exception as exc:  # browser failed to launch, etc. -> FAIL, not a crash
        report = {"url": url, "renders": [], "error": f"{type(exc).__name__}: {exc}"}

    ok, reasons = judge(report)
    report["verdict"] = "PASS" if ok else "FAIL"
    report["reasons"] = reasons
    (outdir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"render proof {report['verdict']}: {url}" + ("" if ok else " -- " + "; ".join(reasons)))
    _append_env_file("GITHUB_OUTPUT", f"verdict={report['verdict']}\n")
    _append_env_file("GITHUB_STEP_SUMMARY", _summary_markdown(url, ok, reasons, report))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
