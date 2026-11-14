<!-- The sections AGENTS.md §12 asks every pull request to cover. Delete a section only when it truly does not apply. -->

## Problem

## How it works
<!-- Data model, flows, concurrency and failure handling. -->

## Migrations, permissions and settings
<!-- New migrations (revision id), permission keys added to FEATURE_PERMS, new settings. "None" if none. -->

## Tests
<!-- Focused tests, the security manifest and the full suite, with counts. Name any failure that also fails on a clean dev. -->

## User interface
<!-- Only for changes to templates, stylesheets or browser code. Checklist from docs/DESIGN_SYSTEM.md §11. -->
- [ ] Built from the macros and components of DESIGN_SYSTEM.md §9; sizes, spacing, colours, radii and timings only from tokens.
- [ ] A new pattern is written into DESIGN_SYSTEM.md in this pull request.
- [ ] Shape says role, at most one primary action, icons from `_icons.html`, no caption under a header title.
- [ ] Empty, loading and error states (§6-3).
- [ ] Seen on a phone (320 and 390 px) and on desktop, in the light and dark themes; screenshots attached.
- [ ] Motion follows §7-1 and stops under reduced motion.
- [ ] `python scripts/design_check.py` and `scripts/lint_changed.sh` pass.

## Out of scope and known limitations
