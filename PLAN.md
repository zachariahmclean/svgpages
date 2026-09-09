# Plan: `svgpages` — vector-true multi-page PDF export for Inkscape Pages

## The idea in one paragraph

A small, pip-installable CLI that takes a master Inkscape SVG using the
**Pages** feature with **linked** child figure SVGs and produces a single,
correctly-sized, **fully vector** multi-page PDF. The core engine is a
**flattener** that replaces each linked `<image href="child.svg">` with the
child's actual vector content inlined as a nested `<svg>` — after which
*either* Inkscape's own CLI *or* headless Chrome can render a vector-true
PDF. Around that core it adds what a publication pipeline needs: preflight
checks (`check`), vector-fidelity verification of the output (`verify`),
page listing (`pages`), and page-subset export.

## Feasibility findings (probed on this machine, 2026-09-08)

All previously-open technical questions were tested with a synthetic 2-page
master (180×130 mm + 90×60 mm) linking a child SVG with a gradient, shapes,
and text. Raw-PDF inspection (page count, MediaBox, `/Subtype /Image`
XObjects, embedded fonts) gave:

| Pipeline | Pages | Sizes correct | Raster images |
|---|---|---|---|
| Inkscape CLI export, **linked** child | 2 | yes | **2 (rasterized!)** |
| Inkscape CLI export, **flattened** child | 2 | yes | **0 — pure vector** |
| Chrome print-to-pdf, HTML `@page` wrapper, linked child | 1 (+1 blank) | yes (±0.2 mm) | **0 — pure vector** |
| Chrome print-to-pdf, raw SVG (no wrapper) | 1 | **no — US Letter** | 0 |

Conclusions that reshape the old plan:

1. **Inkscape ≥1.2 CLI exports multi-page PDFs natively** from a Pages doc
   (`inkscape master.svg --export-type=pdf`) — no per-page cropping, no
   merging needed. The *only* thing wrong with it is that linked images
   rasterize; flattening fixes exactly that. This is the simplest and most
   faithful pipeline (identical renderer to what's on screen while
   authoring), so it becomes the **default backend**.
2. **The Chrome route works too** (first time the render step has actually
   run): with the HTML `@page` wrapper the page size is right and linked
   SVGs stay vector. Kept as `--backend chrome` — useful cross-check and a
   fallback where Inkscape isn't installed. Two quirks confirmed:
   - Printing a raw SVG (no wrapper) defaults to Letter/A4. The HTML
     wrapper with `@page { size: …; margin: 0 }` is mandatory, not a
     fallback.
   - The inline SVG in the wrapper produces a spurious trailing blank page
     unless styled `display: block` (whitespace overflow). Include
     `svg { display: block }` and zeroed body margins.
3. Gotcha for test fixtures: the sodipodi namespace is
   `http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd` (**not**
   `…sodipodi-0.0.dtd`) — with the wrong URI Inkscape silently ignores the
   `namedview`/pages ("unknown type: sodipodi0:namedview" warning) and
   exports a single page.

Environment on this machine: WSL has `/usr/bin/google-chrome` (153) and
`/usr/bin/inkscape` (1.2.2); Windows side has both under
`/mnt/c/Program Files/…`. Both sides testable here.

## Why flattening is the core engine (not just an option)

- It's the *general* fix: once the document is self-contained vector, any
  competent renderer produces a vector PDF. Inkscape backend needs no temp
  path tricks; Chrome backend no longer depends on relative hrefs resolving
  from temp files (temp files can live anywhere).
- The flattened SVG is a useful artifact in itself: a self-contained,
  submittable/archivable figure file, and an automated version of the
  manual "delete + re-import as vector" swap the handoff doc resigned
  itself to. `svgpages flatten` exposes it directly.
- It's effectively an external implementation of the unmerged Inkscape
  "Link (non-rasterized)" MR (!7675) — without building Inkscape.

### Flattening correctness details (the real engineering)

Replace each `<image>` whose href ends in `.svg` with the child document's
root inlined as a **nested `<svg>`** carrying the image's `x`, `y`,
`width`, `height` (nested-svg + child `viewBox` gives the same scaling and
clipping semantics as `<image>`; `preserveAspectRatio` defaults match, copy
it if present; wrap in a `<g>` if the image has a `transform`).

Must-handle issues, in priority order:

1. **ID collisions.** Child SVGs from the same `figstyle.R`/svglite
   pipeline will have colliding ids (svglite clip-path ids are
   content-derived — identical dims → identical ids). Prefix every id in
   each inlined child (`fig1__`, from the source filename + counter) and
   rewrite all references: `url(#id)` in attrs and `style` attrs/elements,
   `href="#id"`, `xlink:href="#id"`.
2. **`<style>` element scoping.** CSS class selectors in a child would leak
   document-wide. Prefix class names like ids. (svglite emits none by
   default; matplotlib/D3 exports do — warn if rewriting looks lossy.)
3. **Strip child editor cruft**: the child's own `sodipodi:namedview`
   (nested pages would confuse Inkscape when opening the flattened file),
   `<metadata>`, XML declaration, DOCTYPE.
4. **Recursion**: children may link further SVGs — recurse with a depth
   limit and cycle detection on resolved paths.
5. **Scope guard**: only local `.svg` hrefs are flattened; raster images
   and `data:` URIs pass through untouched (they're legitimate, e.g.
   micrographs); missing files are a hard `check`/export error.
6. Child without `viewBox`: synthesize one from its width/height.

## Interface

```
svgpages export master.svg out.pdf        # flatten + render, default backend: inkscape
    --backend inkscape|chrome
    --pages 1,3-5                         # subset selection
    --per-page-files out_dir/             # one PDF per page instead of merged
    --no-flatten                          # render with links intact (for comparison)
    --inkscape PATH / --chrome PATH       # override discovery
    --keep-temp                           # keep intermediates for debugging
    --no-verify                           # skip the post-export vector check

svgpages flatten master.svg -o flat.svg   # standalone self-contained SVG
                                          # (records svgpages:src provenance
                                          #  attrs on each inlined child)

svgpages pages master.svg                 # list pages: index, inkscape:label,
                                          # physical size in in/mm — dry run

svgpages check master.svg                 # preflight, no rendering:
                                          #  - every <image href> resolves on disk
                                          #  - warn on absolute paths
                                          #  - warn on rasters below 300 dpi at placed size
                                          #  - fonts referenced vs installed

svgpages verify out.pdf                   # pikepdf: report raster image
                                          # XObjects per page ("did anything
                                          # rasterize?") — report, not error,
                                          # since legit rasters exist
```

Exit codes: 0 clean, 1 hard failure, 2 completed-with-warnings (so a
Makefile in `paper_figures/` can gate on it).

## Backends

- **inkscape (default).** One CLI call on the flattened temp SVG:
  `--export-type=pdf --export-filename=…`. Pages, sizes, and page order come
  out natively (proven above). Page-subset selection: export all then keep
  the selected pages via pikepdf (1.2 has no per-page export flag); revisit
  if the floor ever moves to 1.3+. Pass-through for `--export-text-to-path`
  and `--export-pdf-version` later if needed.
- **chrome.** Per selected page: crop viewBox (port of the proven prototype
  logic), write the HTML `@page` wrapper (with the `display:block` fix),
  `--headless=new --print-to-pdf`, merge with pikepdf. Since input is
  flattened, temp files need no special location.
- **Discovery, WSL-aware**, shared pattern for both tools:
  `$SVGPAGES_INKSCAPE`/`$SVGPAGES_CHROME` → CLI flag → PATH → standard
  Windows/macOS install paths; under WSL also `/mnt/c/Program Files/…`
  with `wslpath -w` translation of file arguments (Windows binaries can't
  read `/home/…` paths).
- **Verify-by-default** after export (skippable), comparing against the
  set of *expected* rasters found during preflight.

## Architecture

```
svgpages/
  pyproject.toml            # console_script: svgpages = svgpages.cli:main
  src/svgpages/
    cli.py                  # argparse subcommands, thin — no logic
    pages.py                # <inkscape:page> geometry, unit math, crop-to-viewBox
                            # (pure; port from prototype, proven)
    flatten.py              # the core engine: inline, id/class namespacing,
                            # recursion, provenance attrs (pure, unit-testable)
    render.py               # backend protocol + inkscape/chrome impls,
                            # tool discovery (incl. WSL), HTML wrapper
    merge.py                # pikepdf merge/subset, page labels from
                            # inkscape:label, PDF metadata
    checks.py               # preflight link/font/dpi checks + PDF verify
  tests/
    fixtures/               # synthetic-but-realistic master + children
    test_pages.py           # units (mm/in/px), multi-page, no-Pages fallback
    test_flatten.py         # id collisions, nested refs, transforms, recursion
    test_render.py          # integration; skipif per-tool availability
  src/export_pages.py       # prototype: delete once M1 reaches parity
```

Dependencies: `lxml`, `pikepdf`, stdlib elsewhere. No Playwright/Selenium.
Python ≥3.10. Fallback when no Inkscape Pages exist: whole doc = one page
(prototype behavior).

## Milestones

**M1 — end-to-end vector export via flatten + Inkscape (the unlock).**
Package skeleton; port `pages.py`; `flatten.py` with id namespacing;
inkscape backend; `export` + `flatten` subcommands. Fixtures first: a
2-page master (mm units, correct sodipodi namespace!) with one page linking
a child SVG that has *deliberately colliding ids* with another child.
Acceptance: `verify`-clean multi-page PDF, correct physical sizes, crisp at
high zoom; flattened SVG opens cleanly in Inkscape.

**M2 — chrome backend + trust tooling.**
Chrome backend (wrapper, discovery incl. WSL, pikepdf merge); `pages`,
`check`, `verify` subcommands; `--pages` selection; exit-code contract;
page labels + PDF metadata. Acceptance: both backends produce
visually-matching, vector-clean output on the fixtures.

**M3 — polish and integration.**
`--per-page-files`; `flatten --in-place`/`refresh` using the recorded
`svgpages:src` provenance (re-flatten only stale children — the "update
linked" workflow); README documenting the `paper_figures/` workflow
(`svgpages check && svgpages export assembly.svg paper_figures.pdf` as a
Makefile target); git housekeeping + tagged release; `pip install -e .` /
`uv tool install`, PyPI optional.

## Open questions (defaults chosen, flag if wrong)

- **Default backend**: `inkscape` chosen for fidelity + simplicity; if
  real-figure tests show cairo mangling something Chrome gets right (e.g.
  specific filter/font cases), the default can flip — both stay supported.
- **Flatten-by-default on export**: yes; `--no-flatten` exists to
  reproduce/compare the rasterizing behavior.
- **Scope**: only the SVG→PDF/preflight layer. `figstyle.R`,
  `paper_figures/` conventions, and per-project export scripts stay outside
  (README documents the fit).
- **Inkscape floor**: ≥1.2 (Pages didn't exist before 1.2 anyway; this
  machine has 1.2.2).
