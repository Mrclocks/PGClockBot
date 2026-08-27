#!/usr/bin/env python3
"""Real-browser proof for the mobile bottom-bar bug (v8.5.40).

Renders the REAL base.html with the REAL panel.css / panel.js in WebKit and
Chromium and checks two things:

geometry
    Across a matrix of phone/tablet/desktop viewports, safe-area insets and page
    lengths, the shell fills the visible viewport, the drawer and its backdrop
    end exactly on the viewport bottom, and closing the drawer restores the
    original geometry (the user's screenshot 3).

behaviour
    While the drawer or a modal is open, the ROOT SCROLLER (html/body) is never
    locked — no overflow:hidden, no touch-action:none, no overscroll-behavior:
    none — yet a background pan still does not move the page. Locking the root
    scroller is what made iOS Safari snap its bottom toolbar to the expanded
    state and stop repainting the strip it had covered, which is the solid bar
    that survived the drawer close.

Needs playwright (not a runtime dependency):
    pip install playwright && playwright install webkit chromium
    python tests/overlay_root_scroll_probe.py [--json]
"""

from __future__ import annotations

import argparse
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app/web/templates"
STATIC = ROOT / "app/web/static"
PORT = 8899
TOL = 1.0

# ─────────────────────────── harness: real templates ───────────────────────────

_CHILD = '{% extends "base.html" %}{% block content %}__BODY__{% endblock %}'

_PAGE_HEAD = '<div class="page-head"><div class="page-title"><h1>داشبورد</h1></div></div>'

BODIES = {
    # Short page: footer visible from first paint. This is the case the user
    # reported as the worst one in the PWA.
    "short": _PAGE_HEAD + '<div class="card"><p>محتوای کوتاه.</p></div>',
    "medium": _PAGE_HEAD
    + "".join(f'<div class="card"><p>کارت {i}</p></div>' for i in range(4)),
    "long": _PAGE_HEAD
    + '<div class="card"><table><thead><tr><th>#</th><th>مبلغ</th></tr></thead><tbody>'
    + "".join(f"<tr><td>{i}</td><td>10,000</td></tr>" for i in range(40))
    + "</tbody></table></div>",
}

PG_PERMS = [
    "pg_overview",
    "pg_users",
    "pg_admins",
    "pg_nodes",
    "pg_groups",
    "pg_hosts",
    "pg_templates",
    "pg_inbounds",
]


class _FakeRequest:
    """The few request attributes base.html touches."""

    class _URL:
        path = "/home"

    class _QP:
        @staticmethod
        def get(_key, default=None):
            return default

    url = _URL()
    query_params = _QP()


def _render(body: str) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"])
    )
    staff = {"username": "admin", "role": "admin", "permissions": [], "pg_permissions": PG_PERMS}
    staff["get"] = staff.copy().get  # base.html calls staff.get(...)
    return env.from_string(_CHILD.replace("__BODY__", body)).render(
        request=_FakeRequest(),
        staff=staff,
        identity={
            "kind": "owner",
            "label": "مالک",
            "can_manage_representatives": True,
            "can_add_representative": True,
        },
        app_version="probe",
        pwa_name="MrClockBot",
        update={"update_available": False},
        inbox_alert=False,
        tickets_unread=False,
        flash_ok=None,
        flash_err=None,
        open_edit=False,
        open_supports_modal=False,
        open_loyalty_settings=False,
        open_finance_settings="",
        open_detail=False,
    )


_PAGES: dict[str, bytes] = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0]
        if path.startswith("/static/"):
            asset = STATIC / path[len("/static/") :]
            if not asset.is_file():
                self.send_error(404)
                return
            ctype = {"css": "text/css", "js": "application/javascript"}.get(
                asset.suffix.lstrip("."), "application/octet-stream"
            )
            self._send(asset.read_bytes(), ctype)
            return
        page = _PAGES.get(path.strip("/"))
        if page is None:
            self.send_error(404)
            return
        self._send(page, "text/html; charset=utf-8")

    def _send(self, data: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


def _serve() -> None:
    for name, body in BODIES.items():
        _PAGES[name] = _render(body).encode("utf-8")
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()


# ───────────────────────────────── geometry ─────────────────────────────────

# name, width, height, (safe top, bottom, left, right)
VIEWPORTS = [
    ("iphone16pm-portrait", 440, 956, (62, 34, 0, 0)),
    # Same device with Safari's bottom toolbar expanded: 130px shorter.
    ("iphone16pm-toolbar-expanded", 440, 826, (62, 34, 0, 0)),
    ("iphone-se-portrait", 375, 667, (20, 0, 0, 0)),
    ("iphone13-portrait", 390, 844, (47, 34, 0, 0)),
    ("iphone13-landscape", 844, 390, (0, 21, 47, 47)),
    ("pixel7-portrait", 412, 915, (24, 0, 0, 0)),
    ("galaxy-fold-narrow", 280, 653, (24, 0, 0, 0)),
    ("ipad-mini-portrait", 744, 1133, (24, 20, 0, 0)),
    ("tablet-900", 900, 1200, (0, 0, 0, 0)),
    # Desktop layout (two-column grid) WITH notch insets — .shell is the single
    # owner of --safe-top there, so both columns must clear the notch.
    ("ipad-landscape-notch", 1194, 834, (24, 20, 0, 0)),
    ("desktop-1280", 1280, 800, (0, 0, 0, 0)),
]

_SAFE_OVERRIDE = """
:root {
  --safe-top: %(t)dpx !important;
  --safe-bottom: %(b)dpx !important;
  --safe-left: %(l)dpx !important;
  --safe-right: %(r)dpx !important;
}
"""

_MEASURE = r"""
() => {
  const box = (el) => {
    if (!el) return null;
    const b = el.getBoundingClientRect();
    return { top: +b.top.toFixed(1), bottom: +b.bottom.toFixed(1), height: +b.height.toFixed(1) };
  };
  const de = document.documentElement;
  const side = document.querySelector('.side');
  return {
    innerH: window.innerHeight,
    scrollY: +window.scrollY.toFixed(1),
    docScrollW: de.scrollWidth,
    docClientW: de.clientWidth,
    body: box(document.body),
    shell: box(document.querySelector('.shell')),
    main: box(document.querySelector('.main')),
    footer: box(document.querySelector('.site-footer')),
    side: box(side),
    sideFoot: box(document.querySelector('.side-foot')),
    backdrop: box(document.querySelector('.side-backdrop')),
    sideScrollH: side ? side.scrollHeight : null,
    sideClientH: side ? side.clientHeight : null,
  };
}
"""


def _check_geometry(
    case: str, page_name: str, mobile: bool, safe_top: int, state: str, m: dict
) -> list[str]:
    vh = m["innerH"]
    bad: list[str] = []

    if m["docScrollW"] > m["docClientW"] + TOL:
        bad.append(f"h_overflow={m['docScrollW'] - m['docClientW']}")

    if state in ("closed", "reclosed"):
        # "reclosed" is the user's screenshot 3: after the drawer closes the
        # shell must fill the viewport again, with no leftover strip.
        if m["shell"]["bottom"] < vh - TOL:
            bad.append(f"shell_short_by={round(vh - m['shell']['bottom'], 1)}")
        if m["body"]["bottom"] < vh - TOL:
            bad.append(f"body_short_by={round(vh - m['body']['bottom'], 1)}")
        if not mobile:
            # Desktop grid: .shell owns --safe-top, so both columns clear the notch.
            for name in ("main", "side"):
                if m[name]["top"] < safe_top - TOL:
                    bad.append(f"{name}_under_notch_by={round(safe_top - m[name]['top'], 1)}")

    if state == "scrolled_to_bottom" and mobile:
        # Mobile scrolls the document, so at max scroll the footer's bottom edge
        # sits on the viewport bottom. Desktop scrolls .main instead.
        if m["footer"]["bottom"] < vh - TOL:
            bad.append(f"footer_gap_at_bottom={round(vh - m['footer']['bottom'], 1)}")

    if state == "open" and mobile:
        side, back = m["side"], m["backdrop"]
        if side["bottom"] < vh - TOL:
            bad.append(f"side_short_by={round(vh - side['bottom'], 1)}")
        if back["bottom"] < vh - TOL:
            bad.append(f"backdrop_short_by={round(vh - back['bottom'], 1)}")
        if abs(side["bottom"] - back["bottom"]) > TOL:
            bad.append("side_backdrop_bottom_mismatch")
        overflows = m["sideScrollH"] > m["sideClientH"] + TOL
        sf = m["sideFoot"]
        if sf and not overflows and abs(sf["bottom"] - vh) > TOL:
            # Drawer content fits, so its footer must land on the viewport bottom.
            bad.append(f"sidefoot_off_bottom_by={round(vh - sf['bottom'], 1)}")
        if sf and overflows and sf["bottom"] <= vh:
            bad.append("sidefoot_unreachable")

    return [f"{case}|{page_name}|{state}: {issue}" for issue in bad]


def run_geometry(browser, engine: str, records: list[dict]) -> list[str]:
    failures: list[str] = []
    for case, w, h, safe in VIEWPORTS:
        mobile = w <= 900
        for page_name in BODIES:
            ctx = browser.new_context(
                viewport={"width": w, "height": h},
                device_scale_factor=2,
                is_mobile=mobile,
                has_touch=mobile,
            )
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/{page_name}", wait_until="load")
            page.add_style_tag(
                content=_SAFE_OVERRIDE
                % {"t": safe[0], "b": safe[1], "l": safe[2], "r": safe[3]}
            )
            page.wait_for_timeout(120)

            states = {"closed": page.evaluate(_MEASURE)}
            page.evaluate("() => window.scrollTo(0, 1e6)")
            page.wait_for_timeout(80)
            states["scrolled_to_bottom"] = page.evaluate(_MEASURE)
            page.evaluate("() => window.scrollTo(0, 0)")
            page.wait_for_timeout(60)

            if mobile:
                page.click("#menu-toggle")
                page.wait_for_timeout(320)
                states["open"] = page.evaluate(_MEASURE)
                page.click("#menu-toggle")
                page.wait_for_timeout(320)
                states["reclosed"] = page.evaluate(_MEASURE)

            for state, m in states.items():
                failures += [
                    f"{engine}|{msg}"
                    for msg in _check_geometry(
                        case, page_name, mobile, safe[0], state, m
                    )
                ]
            records.append(
                {"engine": engine, "case": case, "page": page_name, "states": states}
            )
            ctx.close()
    return failures


# ───────────────────────────────── behaviour ─────────────────────────────────

_LOCK_STATE = r"""
() => {
  const de = document.documentElement, body = document.body;
  const cs = (el) => getComputedStyle(el);
  return {
    htmlOverflow: cs(de).overflowY,
    bodyOverflow: cs(body).overflowY,
    htmlTouchAction: cs(de).touchAction,
    bodyTouchAction: cs(body).touchAction,
    htmlOverscroll: cs(de).overscrollBehaviorY,
    bodyOverscroll: cs(body).overscrollBehaviorY,
    rootScrollable: de.scrollHeight > de.clientHeight + 1,
  };
}
"""


def _root_unlocked(s: dict) -> bool:
    return (
        s["htmlOverflow"] == "visible"
        and s["bodyOverflow"] == "visible"
        and s["htmlTouchAction"] == "auto"
        and s["bodyTouchAction"] == "auto"
        and s["htmlOverscroll"] != "none"
        and s["bodyOverscroll"] != "none"
        and s["rootScrollable"]
    )


def _touch_pan(page, x: int, y: int, dy: int, steps: int = 10) -> None:
    """Synthesize a real finger pan (Chromium only — needs CDP)."""
    cdp = page.context.new_cdp_session(page)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    for i in range(1, steps + 1):
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": "touchMove", "touchPoints": [{"x": x, "y": y - dy * i / steps}]},
        )
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    cdp.detach()


def _settle_at_top(page) -> None:
    """Let any fling from a previous pan die out, then pin the page at the top."""
    page.wait_for_timeout(900)
    page.evaluate("() => window.scrollTo(0, 0)")
    page.wait_for_timeout(500)


def run_behaviour(browser, engine: str) -> list[str]:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: object = "") -> None:
        if not ok:
            failures.append(f"{engine}|{name}: {detail}")

    ctx = browser.new_context(
        viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
        device_scale_factor=2,
    )
    page = ctx.new_page()
    page.goto(f"http://127.0.0.1:{PORT}/long", wait_until="load")
    page.wait_for_timeout(150)
    check("document is the scroller", page.evaluate(_LOCK_STATE)["rootScrollable"])

    page.click("#menu-toggle")
    page.wait_for_timeout(340)
    state = page.evaluate(_LOCK_STATE)
    check("drawer open leaves root scroller unlocked", _root_unlocked(state), state)
    contain = page.evaluate(
        """() => ({
          backdrop: getComputedStyle(document.getElementById('side-backdrop')).touchAction,
          topbar: getComputedStyle(document.querySelector('.topbar')).touchAction,
        })"""
    )
    check(
        "overlays own touch containment",
        contain["backdrop"] == "none" and contain["topbar"] == "none",
        contain,
    )

    if engine == "chromium":  # CDP touch injection
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(80)
        _touch_pan(page, 60, 500, 300)
        page.wait_for_timeout(250)
        y = page.evaluate("() => window.scrollY")
        check("pan on backdrop does not scroll the page", y < 2, f"scrollY={y}")

        before = page.evaluate("() => document.getElementById('sidebar').scrollTop")
        _touch_pan(page, 200, 500, 200)
        page.wait_for_timeout(250)
        after = page.evaluate("() => document.getElementById('sidebar').scrollTop")
        check("pan inside drawer scrolls the drawer", after > before + 5, f"{before}->{after}")

    page.evaluate("() => document.getElementById('side-backdrop').click()")
    page.wait_for_timeout(340)
    state = page.evaluate(_LOCK_STATE)
    closed = page.evaluate(
        "() => !document.getElementById('sidebar').classList.contains('open')"
        " && !document.body.classList.contains('nav-open')"
    )
    check("backdrop tap closes drawer and root stays unlocked", closed and _root_unlocked(state), state)

    if engine == "chromium":
        _touch_pan(page, 200, 500, 300)
        page.wait_for_timeout(250)
        y = page.evaluate("() => window.scrollY")
        check("page scrolls by touch again after close", y > 50, f"scrollY={y}")

    _settle_at_top(page)
    page.evaluate("() => window.openModal('modal-confirm')")
    page.wait_for_timeout(400)
    state = page.evaluate(_LOCK_STATE)
    main_overflow = page.evaluate(
        "() => getComputedStyle(document.querySelector('.main')).overflowY"
    )
    check(
        "modal leaves root scroller and .main unlocked on mobile",
        _root_unlocked(state) and main_overflow == "visible",
        {**state, "mainOverflow": main_overflow},
    )
    if engine == "chromium":
        _touch_pan(page, 60, 700, 300)
        page.wait_for_timeout(250)
        y = page.evaluate("() => window.scrollY")
        check("pan outside modal does not scroll the page", y < 2, f"scrollY={y}")
    ctx.close()

    # Desktop keeps the inner-scroller lock: there .main / .side are the
    # scrollers, and the root never scrolls.
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    page.goto(f"http://127.0.0.1:{PORT}/long", wait_until="load")
    page.wait_for_timeout(150)
    page.evaluate("() => window.openModal('modal-confirm')")
    page.wait_for_timeout(250)
    desktop = page.evaluate(
        """() => ({
          main: getComputedStyle(document.querySelector('.main')).overflowY,
          side: getComputedStyle(document.querySelector('.side')).overflowY,
          html: getComputedStyle(document.documentElement).overflowY,
        })"""
    )
    check(
        "desktop modal freezes .main/.side but not the root",
        desktop["main"] == "hidden" and desktop["side"] == "hidden" and desktop["html"] != "hidden",
        desktop,
    )
    ctx.close()
    return failures


# ─────────────────────────────────── main ───────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="dump every measurement")
    args = ap.parse_args()

    _serve()
    failures: list[str] = []
    records: list[dict] = []
    with sync_playwright() as pw:
        for engine in ("webkit", "chromium"):
            browser = getattr(pw, engine).launch()
            failures += run_geometry(browser, engine, records)
            failures += run_behaviour(browser, engine)
            browser.close()

    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))

    states = sum(len(r["states"]) for r in records)
    if failures:
        print(f"FAILED — {len(failures)} of {states} measured states", file=sys.stderr)
        for line in failures:
            print("  " + line, file=sys.stderr)
        return 1
    print(f"OVERLAY_ROOT_SCROLL_PASSED — {states} states, 2 engines", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
