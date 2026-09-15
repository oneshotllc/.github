"""Unit tests for render_proof.judge(): the PASS/FAIL rules that decide
whether a provisioned site actually renders. Pure dict fixtures -- no
browser, no network.
"""
from __future__ import annotations

import copy

from provisioner.render_proof import is_analytics, is_same_site, judge

SITE = "https://acme-site.fly.dev/"


def render(name: str, **overrides) -> dict:
    r = {
        "name": name,
        "navigation_error": None,
        "status": 200,
        "final_url": SITE,
        "title": "Acme",
        "text_length": 1840,
        "failed_requests": [],
        "console_errors": [],
    }
    r.update(overrides)
    return r


def report(**overrides) -> dict:
    return {
        "url": SITE,
        "renders": [render("mobile", **overrides), render("desktop", **overrides)],
    }


def test_clean_render_passes():
    ok, reasons = judge(report())
    assert ok is True
    assert reasons == []


def test_incident_200_with_css_and_js_on_dead_domain_fails():
    # The page HTML answered 200 and had text, but its assets pointed at a
    # domain with no DNS -- it rendered broken on a real phone.
    dead = [
        {"url": "https://assets.acme-old.example/wp-content/style.css",
         "resource_type": "stylesheet", "error": "net::ERR_NAME_NOT_RESOLVED"},
        {"url": "https://assets.acme-old.example/wp-includes/app.js",
         "resource_type": "script", "error": "net::ERR_NAME_NOT_RESOLVED"},
    ]
    ok, reasons = judge(report(failed_requests=dead))
    assert ok is False
    assert any("stylesheet" in r and "ERR_NAME_NOT_RESOLVED" in r for r in reasons)
    assert any("script" in r and "app.js" in r for r in reasons)
    assert any(r.startswith("mobile:") for r in reasons)
    assert any(r.startswith("desktop:") for r in reasons)


def test_same_site_stylesheet_404_fails():
    missing = [{"url": "https://acme-site.fly.dev/style.css", "resource_type": "stylesheet", "status": 404}]
    ok, reasons = judge(report(failed_requests=missing))
    assert ok is False
    assert any("HTTP 404" in r for r in reasons)


def test_same_site_image_failure_fails():
    broken = [{"url": "https://acme-site.fly.dev/logo.png", "resource_type": "image", "status": 500}]
    ok, _ = judge(report(failed_requests=broken))
    assert ok is False


def test_navigation_name_not_resolved_fails():
    ok, reasons = judge(report(
        navigation_error="Page.goto: net::ERR_NAME_NOT_RESOLVED at https://acme-site.fly.dev/",
        status=None, text_length=0, final_url="chrome-error://chromewebdata/",
    ))
    assert ok is False
    assert any("navigation failed" in r and "ERR_NAME_NOT_RESOLVED" in r for r in reasons)


def test_navigation_connection_closed_fails():
    ok, reasons = judge(report(
        navigation_error="Page.goto: net::ERR_CONNECTION_CLOSED at https://acme-site.fly.dev/",
        status=None, text_length=0,
    ))
    assert ok is False
    assert any("ERR_CONNECTION_CLOSED" in r for r in reasons)


def test_non_200_main_document_fails():
    ok, reasons = judge(report(status=502))
    assert ok is False
    assert any("status 502" in r for r in reasons)


def test_empty_body_fails():
    ok, reasons = judge(report(text_length=0, title=""))
    assert ok is False
    assert any("visible text length 0" in r for r in reasons)


def test_one_broken_view_fails_the_whole_proof():
    rep = report()
    rep["renders"][0]["text_length"] = 3
    ok, reasons = judge(rep)
    assert ok is False
    assert reasons == ["mobile: visible text length 3 < 20"]


def test_third_party_analytics_failure_only_passes():
    blocked = [
        {"url": "https://www.googletagmanager.com/gtag/js?id=G-XXXX",
         "resource_type": "script", "error": "net::ERR_BLOCKED_BY_CLIENT"},
        {"url": "https://www.google-analytics.com/g/collect?v=2",
         "resource_type": "ping", "error": "net::ERR_CONNECTION_RESET"},
        {"url": "https://static.cloudflareinsights.com/beacon.min.js",
         "resource_type": "script", "status": 403},
    ]
    ok, reasons = judge(report(failed_requests=blocked))
    assert ok is True, reasons


def test_third_party_non_critical_types_do_not_fail():
    noise = [
        {"url": "https://fonts.gstatic.com/s/inter.woff2", "resource_type": "font", "error": "net::ERR_FAILED"},
        {"url": "https://cdn.other.example/banner.png", "resource_type": "image", "status": 404},
        {"url": "https://api.other.example/feed", "resource_type": "fetch", "status": 500},
    ]
    ok, reasons = judge(report(failed_requests=noise))
    assert ok is True, reasons


def test_no_renders_or_render_error_fails():
    assert judge({"url": SITE, "renders": []})[0] is False
    ok, reasons = judge({"url": SITE, "renders": [], "error": "Error: browser launch failed"})
    assert ok is False
    assert any("browser launch failed" in r for r in reasons)


def test_judge_does_not_mutate_report():
    rep = report(failed_requests=[{"url": "https://x.example/a.js", "resource_type": "script", "error": "e"}])
    before = copy.deepcopy(rep)
    judge(rep)
    assert rep == before


def test_same_site_and_analytics_helpers():
    assert is_same_site("https://acme-site.fly.dev/a.css", SITE)
    assert is_same_site("https://cdn.acme.org/a.css", "https://acme.org/")
    assert not is_same_site("https://other-site.fly.dev/a.css", SITE)
    assert not is_same_site("https://notacme.org/a.css", "https://acme.org/")
    assert is_analytics("https://region1.google-analytics.com/g/collect")
    assert not is_analytics("https://google-analytics.com.evil.example/x.js")
