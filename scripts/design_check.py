"""Design ratchet for the web panel: a changed template or stylesheet must not gain design-rule findings.

The rules are the checkable part of docs/DESIGN_SYSTEM.md. Existing findings are tolerated; a file may
only keep or lower its count, so every change leaves the panel at least as consistent as it found it.
Rules the panel has fully met (STRICT_RULES) are checked in every tracked file on every run, so they stay at zero.

Usage:
    python scripts/design_check.py              # compare changed files with the merge-base of dev
    python scripts/design_check.py upstream/dev # compare with an explicit ref
    python scripts/design_check.py --details    # also list the findings of failing files
    python scripts/design_check.py --report     # count findings in every file (no comparison)
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = "app/web/templates/"
STYLES = "app/web/static/"
STYLE_EXCLUDES = ("app/web/static/guide/",)
BASE_CANDIDATES = ("upstream/dev", "origin/dev", "dev")
CANCEL_WORDS = ("انصراف", "بستن")
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b")
JINJA_TAG = re.compile(r"\{[%{#].*?[%}#]\}")
JINJA_CANCEL = re.compile(r"ds_foot\([^)]*cancel\s*=\s*true", re.IGNORECASE)
FONT_SIZE = re.compile(r"\bfont-size\s*:\s*[^;}]*?\d(?:px|rem|em)\b|\bfont\s*:[^;}]*?\d(?:px|rem|em)\b")
RADIUS = re.compile(r"\bborder(?:-[a-z]+)*-radius\s*:\s*([^;}]*)")
LENGTH = re.compile(r"(?<![\w.-])(\d*\.?\d+)(?:px|rem|em)\b")
IMPORTANT = re.compile(r"!\s*important")
# An icon is drawn on the 24 or 16 unit grid; a gauge, chart or sparkline has its own viewBox and is data, not an icon.
# A transition of a layout property re-lays the page out every frame; motion uses transform, opacity or clip-path.
TOKEN_DEF = re.compile(r"(--[\w-]+)\s*:([^;}]*)")
# A gap is a spacing token or zero; a raw length here is how two pages end up with different spacing.
RAW_GAP = re.compile(r"(?<![\w-])(?:row-|column-)?gap\s*:\s*([^;}]*)")
LAYOUT_MOTION = re.compile(
    r"\btransition(?:-property)?\s*:[^;}]*?(?<![\w-])(width|height|max-height|min-height|top|right|bottom|left|margin(?:-[a-z]+)?|padding(?:-[a-z]+)?|inset)\b"
)
SVG_TAG = re.compile(r'<svg\b(?=[^>]*viewBox="0 0 (?:24 24|16 16)")')
ICON_SET = "app/web/templates/_icons.html"

# Rules with no finding left anywhere: one finding in any tracked file fails the check.
STRICT_RULES = frozenset({"modal-not-ds", "form-modal-cancel", "inline-style", "bom", "token-loop"})

RULE_HELP = {
    "modal-not-ds": "every .ui-modal also carries .ds (DESIGN_SYSTEM.md §3)",
    "form-modal-cancel": "only a .ds-dialog has «انصراف»; a form modal closes with the header × (§3)",
    "inline-style": "no style=\"…\" in templates; use panel-ds.css classes and tokens (§1, AGENTS.md §9)",
    "raw-colour": "no literal colours in component CSS; use a token such as var(--sel-fg) (§4)",
    "raw-font-size": "text sizes come from the type scale var(--fs-1…5) or var(--control-fs) (§2)",
    "raw-radius": "corner radii come from var(--r-sm), var(--inner-radius), var(--radius) or var(--control-radius) (§2)",
    "important": "no new !important; raise specificity or fix the cascade instead (§9)",
    "token-loop": "a custom property that refers to itself is invalid and makes every use of it fall back to nothing (§1)",
    "bom": "no byte order mark in a template: inside the page it prints an invisible character that takes a line (§1)",
    "inline-svg": "icons come from icon() in _icons.html, not hand-written <svg> in templates (§2)",
    "raw-gap": "gaps come from the spacing tokens (var(--space-*), var(--g*), var(--section-gap)) or 0 (§1)",
    "layout-motion": "only transform, opacity and clip-path animate; a width, height or position transition lays out every frame (§7-1)",
}


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    line: int
    text: str


class _TemplateScanner(HTMLParser):
    """Walks template markup (Jinja tags are plain text to it) and records design findings."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.findings: list[Finding] = []
        self._stack: list[tuple[str, set[str]]] = []
        self._cancel_candidate: tuple[int, set[str]] | None = None
        self._cancel_text: list[str] = []

    def _modal_classes(self) -> set[str] | None:
        for _tag, classes in reversed(self._stack):
            if "ui-modal" in classes:
                return classes
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        # Conditional classes (``ds{% if x %} open{% endif %}``) are read without their Jinja tags.
        classes = set(JINJA_TAG.sub(" ", values.get("class", "")).split())
        line = self.getpos()[0]
        if "ui-modal" in classes and "ds" not in classes:
            self.findings.append(Finding("modal-not-ds", line, values.get("id", "")))
        if "style" in values and not _only_custom_properties(values["style"]):
            self.findings.append(Finding("inline-style", line, values["style"][:60]))
        if tag in ("button", "a") and "data-modal-close" in values and "btn" in classes:
            self._cancel_candidate = (line, classes)
            self._cancel_text = []
        if tag not in VOID_TAGS:
            self._stack.append((tag, classes))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS and self._stack:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._cancel_candidate is not None:
            self._cancel_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("button", "a") and self._cancel_candidate is not None:
            line, _classes = self._cancel_candidate
            label = "".join(self._cancel_text).strip()
            modal = self._modal_classes()
            if modal is not None and "ds-dialog" not in modal and any(word in label for word in CANCEL_WORDS):
                self.findings.append(Finding("form-modal-cancel", line, label))
            self._cancel_candidate = None
        # Tolerate unbalanced markup from Jinja branches: close up to the nearest matching tag.
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break


def _only_custom_properties(style: str) -> bool:
    """A style that only hands data to CSS (``--tag-color: {{ hex }}``, ``--p: 40``) is the way to pass it; layout is not."""
    declarations = [part.strip() for part in JINJA_TAG.sub("x", style).split(";") if part.strip()]
    return bool(declarations) and all(part.startswith("--") for part in declarations)


def template_findings(source: str) -> list[Finding]:
    boms = [Finding("bom", number, "U+FEFF") for number, line in enumerate(source.splitlines(), start=1) if "\ufeff" in line]
    scanner = _TemplateScanner()
    scanner.feed(source)
    scanner.close()
    findings = list(scanner.findings) + boms
    for number, line in enumerate(source.splitlines(), start=1):
        if JINJA_CANCEL.search(line) and not _inside_dialog(source, number):
            findings.append(Finding("form-modal-cancel", number, line.strip()[:60]))
        findings.extend(Finding("inline-svg", number, line.strip()[:60]) for _ in SVG_TAG.finditer(line))
    return findings


def _inside_dialog(source: str, line_number: int) -> bool:
    """Whether the nearest modal opened above this line is a confirmation dialog."""
    above = "\n".join(source.splitlines()[:line_number])
    # Only the modal root counts: ``ui-modal-panel`` and ``ui-modal-head`` sit inside it.
    opens = [
        tokens
        for value in re.findall(r'class="([^"]*)"', above)
        if "ui-modal" in (tokens := JINJA_TAG.sub(" ", value).split())
    ]
    return bool(opens) and "ds-dialog" in opens[-1]


def css_findings(source: str) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(source.splitlines(), start=1):
        code = line.split("/*", 1)[0]
        findings.extend(
            Finding("token-loop", number, match.group(0).strip()[:60])
            for match in TOKEN_DEF.finditer(code)
            if re.search(r"var\(\s*" + re.escape(match.group(1)) + r"\s*[,)]", match.group(2))
        )
        # Token definitions (--name: #hex) are where colours are allowed to live.
        if re.match(r"\s*--[\w-]+\s*:", code):
            continue
        findings.extend(Finding("raw-colour", number, match.group(0)) for match in HEX_COLOUR.finditer(code))
        findings.extend(Finding("raw-font-size", number, match.group(0)[:60]) for match in FONT_SIZE.finditer(code))
        findings.extend(Finding("important", number, code.strip()[:60]) for _ in IMPORTANT.finditer(code))
        findings.extend(Finding("layout-motion", number, match.group(0)[:60]) for match in LAYOUT_MOTION.finditer(code))
        findings.extend(
            Finding("raw-gap", number, match.group(0)[:60])
            for match in RAW_GAP.finditer(code)
            if any(float(length.group(1)) > 0 for length in LENGTH.finditer(re.sub(r"var\([^)]*\)", "", match.group(1))))
        )
        findings.extend(
            Finding("raw-radius", number, match.group(0)[:60])
            for match in RADIUS.finditer(code)
            if any(float(length.group(1)) > 0 for length in LENGTH.finditer(match.group(1)))
        )
    return findings


def is_template(path: str) -> bool:
    return path.startswith(TEMPLATES) and path.endswith(".html")


def is_stylesheet(path: str) -> bool:
    return path.startswith(STYLES) and path.endswith(".css") and not path.startswith(STYLE_EXCLUDES)


def findings_for(path: str, source: str) -> list[Finding]:
    if is_template(path):
        found = template_findings(source)
        return [f for f in found if f.rule != "inline-svg"] if path == ICON_SET else found
    if is_stylesheet(path):
        return css_findings(source)
    return []


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def _base(ref: str | None) -> str:
    candidates = (ref,) if ref else BASE_CANDIDATES
    for candidate in candidates:
        if subprocess.run(["git", "rev-parse", "--verify", "--quiet", candidate], cwd=ROOT, capture_output=True).returncode == 0:
            return _git("merge-base", candidate, "HEAD").strip()
    raise SystemExit("error: no base ref found; pass one explicitly (e.g. upstream/dev)")


def _changed(base: str) -> list[str]:
    tracked = _git("diff", "--name-only", "--diff-filter=AM", base).splitlines()
    untracked = _git("ls-files", "--others", "--exclude-standard").splitlines()
    return sorted({p for p in tracked + untracked if is_template(p) or is_stylesheet(p)})


def _base_source(base: str, path: str) -> str:
    result = subprocess.run(["git", "show", f"{base}:{path}"], cwd=ROOT, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def _summary(findings: list[Finding]) -> str:
    counts = Counter(f.rule for f in findings)
    return ", ".join(f"{rule} {count}" for rule, count in sorted(counts.items())) or "none"


def _report() -> int:
    total = 0
    for path in sorted(_git("ls-files", TEMPLATES, STYLES).splitlines()):
        if not (is_template(path) or is_stylesheet(path)):
            continue
        found = findings_for(path, (ROOT / path).read_text(encoding="utf-8"))
        if found:
            total += len(found)
            print(f"{path}: {_summary(found)}")
    print(f"{total} design findings in tracked templates and stylesheets.")
    return 0


def strict_findings() -> list[tuple[str, Finding]]:
    """Every finding of a rule the whole panel already meets, in every tracked template and stylesheet."""
    found: list[tuple[str, Finding]] = []
    for path in sorted(_git("ls-files", TEMPLATES, STYLES).splitlines()):
        if (is_template(path) or is_stylesheet(path)) and (ROOT / path).is_file():
            found += [(path, f) for f in findings_for(path, (ROOT / path).read_text(encoding="utf-8"))
                      if f.rule in STRICT_RULES]
    return found


def main(argv: list[str]) -> int:
    if "--report" in argv:
        return _report()
    details = "--details" in argv
    refs = [a for a in argv if not a.startswith("--")]
    base = _base(refs[0] if refs else None)
    failed = False
    checked = 0
    for path in _changed(base):
        source_path = ROOT / path
        if not source_path.is_file():
            continue
        after = findings_for(path, source_path.read_text(encoding="utf-8"))
        before = findings_for(path, _base_source(base, path))
        checked += 1
        before_counts, after_counts = Counter(f.rule for f in before), Counter(f.rule for f in after)
        grown = {rule: (before_counts[rule], count) for rule, count in after_counts.items() if count > before_counts[rule]}
        if not grown:
            continue
        failed = True
        for rule, (old, new) in sorted(grown.items()):
            print(f"FAIL {path}: {rule} {old} -> {new} — {RULE_HELP[rule]}")
        if details:
            for finding in after:
                if finding.rule in grown:
                    print(f"  {path}:{finding.line}: {finding.rule}: {finding.text}")
    for path, finding in strict_findings():
        failed = True
        print(f"FAIL {path}:{finding.line}: {finding.rule} — {RULE_HELP[finding.rule]} (this rule is met everywhere)")
    if failed:
        print("Design ratchet failed: a changed file gained findings. See docs/DESIGN_SYSTEM.md.", file=sys.stderr)
        return 1
    print(f"Design ratchet passed: {checked} changed template/stylesheet file(s) did not gain findings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
