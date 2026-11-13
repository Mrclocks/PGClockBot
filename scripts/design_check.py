"""Design ratchet for the web panel: a changed template or stylesheet must not gain design-rule findings.

The rules are the checkable part of docs/DESIGN_SYSTEM.md. Existing findings are tolerated; a file may
only keep or lower its count, so every change leaves the panel at least as consistent as it found it.

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

RULE_HELP = {
    "modal-not-ds": "every .ui-modal also carries .ds (DESIGN_SYSTEM.md §3)",
    "form-modal-cancel": "only a .ds-dialog has «انصراف»; a form modal closes with the header × (§3)",
    "inline-style": "no style=\"…\" in templates; use panel-ds.css classes and tokens (§1, AGENTS.md §9)",
    "raw-colour": "no literal colours in component CSS; use a token such as var(--sel-fg) (§4)",
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
        if "style" in values:
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


def template_findings(source: str) -> list[Finding]:
    scanner = _TemplateScanner()
    scanner.feed(source)
    scanner.close()
    findings = list(scanner.findings)
    for number, line in enumerate(source.splitlines(), start=1):
        if JINJA_CANCEL.search(line) and not _inside_dialog(source, number):
            findings.append(Finding("form-modal-cancel", number, line.strip()[:60]))
    return findings


def _inside_dialog(source: str, line_number: int) -> bool:
    """Whether the nearest modal opened above this line is a confirmation dialog."""
    above = "\n".join(source.splitlines()[:line_number])
    opens = re.findall(r'class="([^"]*\bui-modal\b[^"]*)"', above)
    return bool(opens) and "ds-dialog" in JINJA_TAG.sub(" ", opens[-1]).split()


def css_findings(source: str) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(source.splitlines(), start=1):
        code = line.split("/*", 1)[0]
        # Token definitions (--name: #hex) are where colours are allowed to live.
        if re.match(r"\s*--[\w-]+\s*:", code):
            continue
        findings.extend(Finding("raw-colour", number, match.group(0)) for match in HEX_COLOUR.finditer(code))
    return findings


def is_template(path: str) -> bool:
    return path.startswith(TEMPLATES) and path.endswith(".html")


def is_stylesheet(path: str) -> bool:
    return path.startswith(STYLES) and path.endswith(".css") and not path.startswith(STYLE_EXCLUDES)


def findings_for(path: str, source: str) -> list[Finding]:
    if is_template(path):
        return template_findings(source)
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
    if failed:
        print("Design ratchet failed: a changed file gained findings. See docs/DESIGN_SYSTEM.md.", file=sys.stderr)
        return 1
    print(f"Design ratchet passed: {checked} changed template/stylesheet file(s) did not gain findings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
