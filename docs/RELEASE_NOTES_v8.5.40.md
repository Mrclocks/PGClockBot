# v8.5.40 — The bottom bar, root-caused: never lock the root scroller

This release removes the actual cause of the solid bar at the bottom of mobile
pages. Versions v8.5.20 – v8.5.39 all attacked the *symptom* (viewport units,
safe-area insets, background colours, `color-scheme`, glass effects, footer
heights). The cause was in a different file entirely.

## The bug, as reported

1. Page renders correctly, content fills the screen.
2. Open the sidebar → a solid bar appears at the bottom **and the drawer does
   not reach the bottom of the screen**.
3. Close the sidebar → **the bar stays**, and the content is now short.

Worst in iOS Safari, milder in the PWA, and in the PWA only on short pages where
the footer is visible from first paint.

## Root cause

Opening an overlay locked the **root scroller**:

```css
body.nav-open        { overscroll-behavior: none; touch-action: none; }
html.modal-open,
body.modal-open      { overflow: hidden !important; overscroll-behavior: none; }
```

That is the standard "stop the background from scrolling" trick, and on iOS
Safari it is a trap:

- Safari sizes the visible viewport around a **bottom toolbar that expands and
  collapses with scrolling**. It collapses the toolbar when you scroll down and
  re-expands it when you scroll up or tap it.
- The moment the root scroller becomes unscrollable, Safari **snaps the toolbar
  to its expanded state** and the visible viewport shrinks by the toolbar's
  height. The page keeps its `100svh` layout size, so the strip the toolbar now
  covers is simply never repainted — a solid bar. The drawer is laid out against
  the *layout* viewport, so it visibly stops short of the screen bottom (symptom 2).
- Safari only retracts the toolbar again **on a real page scroll**. After the
  drawer closes, the page is back at the top and nothing scrolls, so the toolbar
  and the dead strip stay (symptom 3). Tapping the page or scrolling once makes
  it snap back — which is exactly what made this bug look intermittent and
  impossible to pin down.

In the PWA there is no Safari toolbar, so only the milder short-page variant
showed, matching the report.

## Fix — one rule

**Nothing in the stylesheet or the scripts may ever make `html` or `body`
unscrollable.** Background containment belongs to the overlay that is on top:

| Element | Job |
| --- | --- |
| `.side-backdrop` | full-bleed `inset: 0`, `touch-action: none`, `overscroll-behavior: contain` |
| `.topbar` (while `body.nav-open`) | `touch-action: none` — it is the only chrome painted *above* the backdrop |
| `.ui-modal-backdrop` | same containment for modals |
| `.main` / `.side` on **desktop only** | still frozen during modals: there *they* are the scrollers and the root never scrolls |

`touch-action: none` still delivers taps, so tap-to-close keeps working, and
`overscroll-behavior: contain` keeps a drawer fling from chaining to the page.

## Dead code and parallel logic removed

- `html.ios-standalone` / `html.ios-safari` classes — set on boot, matched by nothing
- the `html.modal-open` class, its `MutationObserver` sync, and the `modalScrollY`
  save/restore that only existed because the document used to be frozen
- `-webkit-fill-available` from the `html` / `body` / `.shell` fill chain: it
  computes to `stretch` in Chromium and silently collapsed the chain, so two
  engines took two different paths. The ladder is now `100%` → `100svh` only.
- the duplicate mobile `.side-backdrop` override (geometry now defined once)
- `transform: none` on the mobile drawer (it is slid with `right`, never
  `transform`, because a transformed fixed box becomes its own containing block
  on WebKit and stops tracking the viewport bottom)
- `--safe-top` was applied by both `.shell` and `.side` on desktop; `.shell` owns
  it now
- unused `.risk-dot` CSS

## Verification

`tests/overlay_root_scroll_probe.py` renders the real `base.html` with the real
`panel.css` / `panel.js` in **WebKit and Chromium** and checks:

- **geometry** — 10 viewports (iPhone SE → 16 Pro Max, portrait *and* landscape,
  Pixel, narrow fold, iPad, tablet, desktop) with real safe-area insets, one of
  them sized to Safari's *expanded-toolbar* height, × 3 page lengths × 4 states
  (closed, scrolled to the end, drawer open, drawer re-closed) = **228 measured
  states, 0 violations**. The shell fills the viewport, the drawer and backdrop
  share the viewport bottom edge, and re-closing restores the original geometry.
- **behaviour** — with the drawer or a modal open: `html`/`body` keep
  `overflow: visible`, `touch-action: auto`, `overscroll-behavior: auto` and the
  document stays scrollable, yet an injected finger pan on the background does
  not move the page, while a pan inside the drawer scrolls the drawer. After
  closing, the page scrolls by touch again. Desktop modals still freeze
  `.main` / `.side`.
- **pixels** — screenshots of all three reported states in both themes: the
  bottom 8 device-pixel rows are a single flat background colour, and the
  re-closed screenshot is **pixel-identical** to the pre-open one below the
  topbar.

The invariants are pinned by `tests/test_scroll_rubber_band_fix.py`
(`test_open_overlays_never_lock_the_root_scroller`), and CSS assertions across
the suite now use `tests/css_blocks.py`, which extracts a rule by brace
matching instead of `str.split`, so a test can no longer silently read the wrong
block and pass.

SW: `pgclock-shell-v31`. Restore: `v8.5.34`.
