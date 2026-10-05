"""Panel-wide CSRF hardening after v10 double-submit token."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_panel_js_stamps_csrf_before_programmatic_submit():
    panel = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
    assert "function submitFormWithCsrf" in panel
    assert "ensureCsrfField(form);" in panel
    assert "xhr.setRequestHeader('X-CSRF-Token'" in panel or 'xhr.setRequestHeader("X-CSRF-Token"' in panel
    # Bulk ops used naked form.submit() which bypasses the submit listener.
    assert "function postBulk" in panel
    post_idx = panel.index("function postBulk")
    chunk = panel[post_idx : post_idx + 2500]
    assert "ensureCsrfField(form)" in chunk
    assert "form.submit()" in chunk


def test_csrf_reject_json_covers_backup_and_ajax():
    src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
    assert 'path_now.startswith("/backup/")' in src
    assert 'path_now.startswith("/api/")' in src
    assert "x-requested-with" in src
    assert 'not path_now.startswith("/api/mini")' in src


def test_backup_restore_parses_non_json_safely():
    html = (ROOT / "app/web/templates/_settings_backup.html").read_text(encoding="utf-8")
    assert "JSON.parse(text)" in html
    assert "نشست امنیتی منقضی شده" in html
    assert "panelEnsureCsrfField" in html


def test_release_notes_mention_csrf_sweep():
    notes = (ROOT / "app/services/release_notes.py").read_text(encoding="utf-8")
    assert '"0.1.1"' in notes
    assert '"0.1.10"' in notes
    from app.version import __version__

    ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert ver == __version__ == "0.1.10"
