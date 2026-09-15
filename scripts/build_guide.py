#!/usr/bin/env python3
"""Build the Persian PGClockBot guide into app/web/static/guide/.

Sources:
  docs/guide/pages/*.md
  docs/guide/theme/*
  app/services/guide_catalog.py

Usage:
  python scripts/build_guide.py

The built site is committed so production installs get /help without extra tools.
Re-run this script after editing Markdown, then commit the updated static files.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import sys
from collections import OrderedDict
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "docs" / "guide" / "pages"
THEME = ROOT / "docs" / "guide" / "theme"
OUT = ROOT / "app" / "web" / "static" / "guide"
WEB_STATIC = ROOT / "app" / "web" / "static"

sys.path.insert(0, str(ROOT))
from app.services.guide_catalog import NAV_ORDER, TOPICS, nav_topics  # noqa: E402


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    raw = text[3:end].strip()
    body = text[end + 4 :].lstrip("\n")
    meta: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip().strip("\"'")
    return meta, body


def inline_md(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        text,
    )
    return text


def md_to_html(src: str) -> str:
    lines = src.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("```"):
            lang = line[3:].strip()
            i += 1
            buf: list[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            code = html.escape("\n".join(buf))
            cls = f' class="language-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{code}</code></pre>")
            continue
        m = re.match(r"^:::(tip|warn|error|info)\s*$", line.strip())
        if m:
            kind = m.group(1)
            labels = {
                "tip": "نکته",
                "warn": "هشدار",
                "error": "خطای رایج",
                "info": "راهنما",
            }
            i += 1
            buf = []
            while i < len(lines) and lines[i].strip() != ":::":
                buf.append(lines[i])
                i += 1
            i += 1
            inner = md_to_html("\n".join(buf))
            out.append(
                f'<div class="guide-callout guide-callout--{kind}">'
                f'<strong class="guide-callout-label">{labels[kind]}</strong>'
                f"{inner}</div>"
            )
            continue
        if line.startswith("### "):
            title = line[4:].strip()
            out.append(f'<h3 id="{slugify(title)}">{inline_md(title)}</h3>')
            i += 1
            continue
        if line.startswith("## "):
            title = line[3:].strip()
            out.append(f'<h2 id="{slugify(title)}">{inline_md(title)}</h2>')
            i += 1
            continue
        if line.startswith("# "):
            i += 1
            continue
        if re.match(r"^[-*] ", line):
            items = []
            while i < len(lines) and re.match(r"^[-*] ", lines[i]):
                items.append(f"<li>{inline_md(lines[i][2:].strip())}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        if re.match(r"^\d+\. ", line):
            items = []
            while i < len(lines) and re.match(r"^\d+\. ", lines[i]):
                text = re.sub(r"^\d+\.\s*", "", lines[i]).strip()
                items.append(f"<li>{inline_md(text)}</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            continue
        if line.startswith("> "):
            buf = []
            while i < len(lines) and lines[i].startswith("> "):
                buf.append(lines[i][2:])
                i += 1
            out.append(f"<blockquote>{md_to_html(chr(10).join(buf))}</blockquote>")
            continue
        if _is_table_row(line) and i + 1 < len(lines) and _is_table_sep(lines[i + 1]):
            header = _split_table_row(line)
            i += 2  # skip header + separator
            rows: list[list[str]] = []
            while i < len(lines) and _is_table_row(lines[i]):
                rows.append(_split_table_row(lines[i]))
                i += 1
            thead = "<tr>" + "".join(f"<th>{inline_md(c)}</th>" for c in header) + "</tr>"
            tbody = "".join(
                "<tr>" + "".join(f"<td>{inline_md(c)}</td>" for c in row) + "</tr>"
                for row in rows
            )
            out.append(
                '<div class="guide-table-wrap" role="region" '
                'aria-label="جدول" tabindex="0">'
                f"<table><thead>{thead}</thead><tbody>{tbody}</tbody></table>"
                "</div>"
            )
            continue
        buf = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]):
            buf.append(lines[i])
            i += 1
        out.append(f"<p>{inline_md(' '.join(x.strip() for x in buf))}</p>")
    return "\n".join(out)


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and s.count("|") >= 3


def _is_table_sep(line: str) -> bool:
    s = line.strip()
    if not _is_table_row(s):
        return False
    cells = _split_table_row(s)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", c.replace(" ", "")) for c in cells)


def _split_table_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _starts_block(line: str) -> bool:
    return bool(
        line.startswith("#")
        or line.startswith("```")
        or line.startswith(":::")
        or line.startswith("> ")
        or re.match(r"^[-*] ", line)
        or re.match(r"^\d+\. ", line)
        or (_is_table_row(line))
    )


def slugify(text: str) -> str:
    s = text.strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^\w\u0600-\u06ff\-]+", "", s, flags=re.UNICODE)
    return s or "section"


ROLE_FA = {
    "owner": "مالک",
    "admin": "ادمین",
    "reseller": "نماینده",
    "pg_staff": "ادمین پاسارگارد",
    "principal": "سلسله‌مراتب",
}

# Stroke icons (24×24) — shared by sidebar + page titles
ICON_PATHS: dict[str, str] = {
    "rocket": '<path d="M5 15c1.5 2 4 4 7 4"/><path d="M9 19c0-3 2-5 4-6"/><path d="M14 13c3-1 5-4 6-8-3 1-6 3-8 6"/><path d="M9 15l-3 3"/><path d="M12 9l1.5-1.5"/>',
    "shield": '<path d="M12 3 5 6v5c0 4.2 2.8 7.2 7 8.5 4.2-1.3 7-4.3 7-8.5V6l-7-3z"/>',
    "home": '<path d="M4 10.5 12 4l8 6.5V20a1 1 0 0 1-1 1h-5v-6H10v6H5a1 1 0 0 1-1-1v-9.5z"/>',
    "bell": '<path d="M12 4a5 5 0 0 1 5 5v2.2l1.8 3.3H5.2L7 11.2V9a5 5 0 0 1 5-5z"/><path d="M10 18a2 2 0 0 0 4 0"/>',
    "sliders": '<path d="M4 7h9M17 5v4M19 7h1"/><path d="M4 12h3M11 10v4M13 12h7"/><path d="M4 17h11M19 15v4M21 17h-1"/>',
    "gauge": '<path d="M12 3 4.5 6.5v5.2c0 4.7 3.2 8 7.5 9.3 4.3-1.3 7.5-4.6 7.5-9.3V6.5L12 3z"/><path d="M9 12l2 2 4-4"/>',
    "users": '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/><circle cx="17" cy="9" r="2.4"/><path d="M15.2 19c.4-2.2 2-3.8 4.3-4.2"/>',
    "wallet": '<path d="M7 7h13l-1.2 9.2a2 2 0 0 1-2 1.8H9.4a2 2 0 0 1-2-1.7L6 4H3"/><circle cx="10" cy="20" r="1.2"/><circle cx="17" cy="20" r="1.2"/>',
    "layers": '<path d="M4 7h16M4 12h16M4 17h10"/><path d="M18 15v4M16 17h4"/>',
    "life-buoy": '<path d="M5 6h14a2 2 0 0 1 2 2v3a2 2 0 0 0 0 4v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-3a2 2 0 0 0 0-4V8a2 2 0 0 1 2-2z"/><path d="M12 9v6"/>',
    "megaphone": '<path d="M4 10v4h3l5 4V6L7 10H4z"/><path d="M16.5 8.5a5 5 0 0 1 0 7M18.8 6.2a8.5 8.5 0 0 1 0 11.6"/>',
    "star": '<path d="M12 3l2.2 4.5 5 .7-3.6 3.5.9 5L12 14.8 7.5 16.7l.9-5L4.8 8.2l5-.7L12 3z"/>',
    "braces": '<path d="M7 7h10v10H7z"/><path d="M10 10h4M10 14h2"/><path d="M4 12h2M18 12h2"/>',
    "bot": '<rect x="4" y="8" width="16" height="11" rx="2.5"/><path d="M12 8V5H9"/><path d="M2 14h2M20 14h2"/><circle cx="9.5" cy="13.5" r="1.1"/><circle cx="14.5" cy="13.5" r="1.1"/><path d="M9.5 17h5"/>',
    "network": '<circle cx="8" cy="9" r="3"/><circle cx="16" cy="9" r="3"/><path d="M2.8 19a5.2 5.2 0 0 1 10.4 0M10.8 19a5.2 5.2 0 0 1 10.4 0"/>',
    "inbox": '<path d="M4 8h16l-1.2 10.2A2 2 0 0 1 16.8 21H7.2a2 2 0 0 1-2-1.8L4 8z"/><path d="M8 8V6a4 4 0 0 1 8 0v2"/>',
    "server": '<rect x="3" y="4" width="7" height="7" rx="1.5"/><rect x="14" y="4" width="7" height="7" rx="1.5"/><rect x="8.5" y="13" width="7" height="7" rx="1.5"/>',
    "user": '<circle cx="12" cy="8" r="3.2"/><path d="M5 19a7 7 0 0 1 14 0"/>',
    "key": '<path d="M12 3 5 6v5c0 4.2 2.8 7.2 7 8.5 4.2-1.3 7-4.3 7-8.5V6l-7-3z"/>',
    "cpu": '<rect x="3" y="4" width="7" height="7" rx="1.5"/><rect x="14" y="4" width="7" height="7" rx="1.5"/><rect x="8.5" y="13" width="7" height="7" rx="1.5"/>',
    "boxes": '<path d="M12 3 4.5 6.5v5.2c0 4.7 3.2 8 7.5 9.3 4.3-1.3 7.5-4.6 7.5-9.3V6.5L12 3z"/>',
    "git-branch": '<path d="M4 12h16M12 4v16"/><circle cx="12" cy="12" r="3"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3.5 3 14.5 0 18M12 3c-3 3.5-3 14.5 0 18"/>',
    "layout": '<path d="M4 7h16M4 12h16M4 17h10"/><path d="M18 15v4M16 17h4"/>',
    "filter": '<path d="M4 6h16l-5.5 7v5l-5 2v-7L4 6z"/>',
    "wrench": '<path d="M14.5 5.5a4 4 0 0 1 4 4L12 16l-3-3 6.5-6.5z"/><path d="M9 13 5 17l2 2 4-4"/>',
    "upload": '<path d="M12 16V6M8.5 9.5 12 6l3.5 3.5"/><path d="M5 18h14"/>',
    "book": '<path d="M5 5.5A2.5 2.5 0 0 1 7.5 3H19v16H7.5A2.5 2.5 0 0 0 5 21.5z"/><path d="M5 5.5V21.5"/>',
}


def icon_svg(name: str) -> Markup:
    paths = ICON_PATHS.get(name) or ICON_PATHS["book"]
    return Markup(
        f'<svg class="guide-ico" viewBox="0 0 24 24" aria-hidden="true" fill="none" '
        f'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
        f'stroke-linejoin="round">{paths}</svg>'
    )


GROUP_ICONS: dict[str, str] = {
    "شروع": "rocket",
    "وب پنل": "layout",
    "پنل ربات": "bot",
    "پاسارگارد": "server",
    "کمک": "life-buoy",
    "سایر": "book",
}

# Match web-panel chrome: bot = orange, Pasarguard = blue
GROUP_TONES: dict[str, str] = {
    "پنل ربات": "bot",
    "پاسارگارد": "pg",
}


def nav_groups_list():
    groups: OrderedDict[str, list] = OrderedDict()
    for t in nav_topics():
        g = t.get("nav_group") or "سایر"
        item = dict(t)
        item["icon_svg"] = icon_svg(item.get("icon") or "book")
        item["tone"] = GROUP_TONES.get(g, "")
        groups.setdefault(g, []).append(item)
    return [
        {
            "title": title,
            "tone": GROUP_TONES.get(title, ""),
            "icon_svg": icon_svg(GROUP_ICONS.get(title, "book")),
            "topics": items,
        }
        for title, items in groups.items()
    ]


def roles_label(roles: list[str]) -> str:
    return "، ".join(ROLE_FA.get(r, r) for r in roles)


def main() -> int:
    if not PAGES.is_dir():
        print(f"missing pages dir: {PAGES}", file=sys.stderr)
        return 1

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    shutil.copy2(THEME / "guide.css", OUT / "guide.css")
    shutil.copy2(THEME / "guide.js", OUT / "guide.js")
    shutil.copy2(WEB_STATIC / "fonts.css", OUT / "fonts.css")
    fonts_dir = WEB_STATIC / "fonts"
    if fonts_dir.is_dir():
        shutil.copytree(fonts_dir, OUT / "fonts")
    for name in ("logo-64.png", "logo.png"):
        src = WEB_STATIC / name
        if src.is_file():
            shutil.copy2(src, OUT / name)

    fonts_css = (OUT / "fonts.css").read_text(encoding="utf-8")
    fonts_css = fonts_css.replace('url("/static/fonts/', 'url("fonts/')
    fonts_css = fonts_css.replace("url('/static/fonts/", "url('fonts/")
    (OUT / "fonts.css").write_text(fonts_css, encoding="utf-8")

    env = Environment(
        loader=FileSystemLoader(str(THEME)),
        autoescape=select_autoescape(["html"]),
    )

    page_files = {p.stem: p for p in PAGES.glob("*.md")}
    built: list[dict] = []
    search_items: list[dict] = []
    groups = nav_groups_list()

    for tid in NAV_ORDER:
        meta = TOPICS[tid]
        path = page_files.get(tid) or page_files.get(meta["slug"])
        if not path:
            print(f"warn: no markdown for topic {tid}", file=sys.stderr)
            body_src = "## به‌زودی\n\nآموزش این بخش به‌زودی تکمیل می‌شود.\n"
        else:
            _fm, body_src = parse_frontmatter(path.read_text(encoding="utf-8"))
        body_html = md_to_html(body_src)
        built.append(
            {
                "id": tid,
                "slug": meta["slug"],
                "title": meta["title"],
                "summary": meta["summary"],
                "panel": meta.get("panel") or "",
                "roles": meta.get("roles") or [],
                "icon": meta.get("icon") or "book",
                "icon_svg": icon_svg(meta.get("icon") or "book"),
                "body_html": Markup(body_html),
                "body_text": re.sub(r"<[^>]+>", " ", body_html),
                "aliases": meta.get("aliases") or [],
            }
        )

    page_tpl = env.get_template("page.html")
    for idx, page in enumerate(built):
        prev = built[idx - 1] if idx > 0 else None
        nxt = built[idx + 1] if idx + 1 < len(built) else None
        dest = OUT / page["slug"]
        dest.mkdir(parents=True, exist_ok=True)
        html_out = page_tpl.render(
            title=page["title"],
            summary=page["summary"],
            body_html=page["body_html"],
            page_id=page["id"],
            icon_svg=page["icon_svg"],
            page_tone=GROUP_TONES.get((TOPICS[page["id"]].get("nav_group") if page["id"] in TOPICS else ""), ""),
            root="../",
            nav_groups=groups,
            panel_path=page["panel"],
            panel_href=page["panel"] or "#",
            roles_label=roles_label(page["roles"]),
            prev=prev,
            next=nxt,
        )
        (dest / "index.html").write_text(html_out, encoding="utf-8")
        search_items.append(
            {
                "id": page["id"],
                "title": page["title"],
                "summary": page["summary"],
                "aliases": page["aliases"],
                "body": page["body_text"][:4000],
                "href": f"{page['slug']}/index.html",
            }
        )

    index_md = PAGES / "index.md"
    if index_md.is_file():
        _, index_body = parse_frontmatter(index_md.read_text(encoding="utf-8"))
        index_html_body = Markup(md_to_html(index_body))
    else:
        index_html_body = Markup("<p>از فهرست کناری یا کارت‌های زیر موضوع را انتخاب کنید.</p>")

    featured_ids = ["start", "plans", "finance", "users", "resellers", "troubleshooting"]
    featured = [p for p in built if p["id"] in featured_ids]
    home_html = env.get_template("home.html").render(
        root="./",
        nav_groups=groups,
        body_html=index_html_body,
        featured=featured,
    )
    (OUT / "index.html").write_text(home_html, encoding="utf-8")

    (OUT / "search-index.json").write_text(
        json.dumps(search_items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    catalog = {
        tid: {
            "title": TOPICS[tid]["title"],
            "summary": TOPICS[tid]["summary"],
            "slug": TOPICS[tid]["slug"],
            "panel": TOPICS[tid].get("panel") or "",
        }
        for tid in NAV_ORDER
    }
    (OUT / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    for page in built:
        local = [{**it, "href": f"../{it['href']}"} for it in search_items]
        (OUT / page["slug"] / "search-index.json").write_text(
            json.dumps(local, ensure_ascii=False),
            encoding="utf-8",
        )

    print(f"built {len(built)} pages → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
