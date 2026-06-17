# AGENTS.md — maintaining the styling system

This project uses the **Bitwise design system** (an editorial/data aesthetic: slate greys,
one brand green, an italic variable serif for hero numerals, monospace for every label and
figure). The entire look is **token-driven** and lives in a few files. Read this before
adding or changing any UI so new work stays consistent with the rest of the dashboard.

## Where styling lives

| File | What |
|---|---|
| `site.css` | The whole system, in order: **tokens → base/chrome → components → chart & live-motion styles**. Start here. |
| `styles/fonts.css` | `@font-face` for the three families. |
| `app.js` | Renders components and builds every chart as inline SVG. Style-relevant constants live near the top. |
| `index.html` | Shell + tab panes (DOM siblings toggled by the `hidden` attribute). |

## The rules (don't break these)

1. **Tokens only.** Never hardcode a hex, px font-size, shadow, or radius. Use the CSS
   custom properties: `--slate-1…16`, `--brand-1…8` (green, HSL 146), `--peer*` (purple,
   HSL 262), semantic `--bg / --bg-alt / --bg-elev / --text / --text-strong / --text-muted /
   --border / --primary`, the `--t-*` type scale, `--r-sm|md|pill`, `--shadow-card|nav`,
   `--t-short|base` motion. If something's missing, **add a semantic token**, don't inline a value.
2. **Dark mode is a bespoke remap.** `.theme-dark` redefines the slate scale + peer/primary/
   shadows by hand — it is *not* an inversion. Any new color token needs **both** a light value
   (`:root`) and a dark value (`.theme-dark`). Brand green is constant across themes.
3. **Type.** `--serif` (Items) for display/stat numerals only, at `font-weight:300; font-stretch:550%`,
   italic `<em>` for emphasis. `--sans` (PP Neue Montreal) for body/UI. `--mono` (PP Neue Montreal
   Mono) for kickers, labels, and **all numerals** in tables/KPIs/charts.
4. **Numerals are tabular.** Anything numeric carries `font-feature-settings:"tnum"` (via `.mono`
   or the table/KPI/chart classes). Misaligned digits are a bug.
5. **Kickers, not headlines, scaffold sections.** Every section/card leads with a mono-uppercase
   `.kicker` in one of two forms: `— Label` or `Component · Thing · detail`.
6. **Editorial voice, terse.** Sentence case; fragments over sentences; one italic noun in display
   titles. Data decks are terse mono stamps (`.mono-stamp`), e.g. `bid 0.250 ETH · 7.0k blocks/day`.
   Specific numerals, never marketing words ("best-in-class", "robust", etc.). No emoji — use SVG
   icons, mono pills, or text arrows (`↑ ↓ → ·`).
7. **Shadows & radii are restricted.** `box-shadow` is only `--shadow-card`, `--shadow-nav`, or the
   documented insets. `border-radius` is only `--r-sm`, `--r-md`, or `--r-pill`.

## Charts (all hand-built SVG — no chart libraries)

- Build a chart as a function that sets `viewBox="0 0 1100 H"` and writes an SVG string; style
  via CSS classes (`.chart-svg …`, `.line.p50/p90/p99`, `.bar-*`, `.hcell.hc-0…5`, etc.), not
  inline colors. Reuse `attachLineHover` for crosshair tooltips.
- **Color vocabulary is the palette**: brand green = the subject, peer purple = secondary,
  slate = neutral/other, dashed faint = median/network. Heatmaps use the sequential green ramp
  `.hc-0…hc-5`. Don't introduce a new chart color without adding a token and a reason.
- **Legibility**: charts render their `viewBox` scaled to the card width — narrow (half-width)
  cards shrink the in-SVG text. Prefer **full-width** chart cards. Smooth noisy daily series
  (`rollingMean`) and drop per-cell strokes on dense heatmaps so values read as bands.
- No spinners / no load shimmers; the boot overlay is a static fade. Animations only on tab
  fade-in and deliberate live-motion (e.g. a block "arrived" slide) — gate animations so they
  fire only on real state changes, not on every re-render.

## Adding or changing UI — checklist

1. Reuse an existing class/pattern before writing new CSS; match the surrounding code's idiom.
2. New token? Add light + dark values. New chart color? Justify it against the vocabulary.
3. Numerals → mono + tnum. Headings → serif with one `<em>`. Section → `.kicker`.
4. Keep tab panes as DOM siblings; wire tab routing **before** any data `await` so tabs work
   even if a fetch fails.
5. **Verify visually**: `python3 server.py`, then screenshot in headless Chrome in **both**
   light and dark before claiming done. Check `node --check app.js`.

## Provenance

The system derives from the Bitwise `bitwise-dashboard` skill and its reference dashboard. When
in doubt about a pattern (a component class, a voice rule, a token), prefer consistency with what
already exists in `site.css` over inventing something new.
