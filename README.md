# svgpages

Vector-true multi-page PDF export for Inkscape **Pages** documents with
**linked** SVG figures.

Inkscape's own PDF export rasterizes any linked `<image href="child.svg">`
(a [long-standing limitation](https://gitlab.com/inkscape/inbox/-/issues/2103)),
typically at ~96 dpi — far below journal requirements. `svgpages` fixes this
by **flattening**: each linked SVG is replaced by its actual vector content,
inlined as a nested `<svg>` with all ids namespaced to avoid collisions.
The flattened document then exports through Inkscape's CLI as a native
multi-page, fully vector PDF.

## Usage

```console
$ svgpages export assembly.svg paper_figures.pdf
2 page(s) in assembly.svg:
  page01 (Figure 1): 180.0 x 130.0 mm
  page02 (Figure 2): 90.0 x 60.0 mm
wrote paper_figures.flat.svg
wrote paper_figures.pdf

$ svgpages flatten assembly.svg -o assembly_flat.svg   # flatten only
$ svgpages pages assembly.svg          # list pages: sizes and labels (dry run)
$ svgpages check assembly.svg          # preflight: broken/absolute links,
                                       # low-dpi rasters, missing fonts,
                                       # Inkscape <use href> quirk
$ svgpages verify paper_figures.pdf    # raster images per PDF page
```

`export` writes the flattened, self-contained SVG next to the PDF by
default — if something needs a manual fix, adjust the `.flat.svg` in
Inkscape and re-export it (`svgpages export paper_figures.flat.svg out.pdf`
is a valid round trip).

## Command reference

### `svgpages export MASTER.svg OUTPUT.pdf`

Flatten and render a master SVG to a multi-page vector PDF.

- `--backend inkscape|chrome` — rendering backend (default: `inkscape`).
  `inkscape` exports all pages natively in one call, with rendering
  identical to what you see while authoring. `chrome` prints each page
  through headless Chrome via an HTML `@page` wrapper and merges the
  results; useful as a cross-check or where Inkscape isn't installed.
- `--pages SPEC` — export only a subset of pages, e.g. `--pages 1,3-5`.
- `--flat-svg PATH` — where to write the flattened SVG that accompanies
  the PDF (default: the output PDF's name with a `.flat.svg` suffix).
- `--no-flat-svg` — don't write the flattened SVG next to the PDF.
- `--no-flatten` — render with linked images intact. Linked SVGs will
  rasterize; useful only for comparison/debugging.
- `--no-verify` — skip the post-export raster check of the PDF.
- `--inkscape PATH` — Inkscape executable to use (also settable via the
  `SVGPAGES_INKSCAPE` environment variable). Windows binaries work from
  WSL, e.g. `/mnt/c/Program Files/Inkscape/bin/inkscape.exe`.
- `--chrome PATH` — Chrome/Chromium executable to use (also
  `SVGPAGES_CHROME`); Windows Chrome works from WSL as well.
- `--keep-temp` — keep intermediate files (cropped pages, wrapper HTML,
  per-page PDFs) for debugging.

Exported PDFs carry page labels from `inkscape:label` and a title from the
master filename. After export, the PDF's raster count is verified against
the source's raster references; an excess means something got rasterized.

### `svgpages flatten MASTER.svg`

Write a self-contained SVG with all linked SVGs inlined as vector content.
Both file links and embedded `data:image/svg+xml` URIs (base64 or
URL-encoded, as produced by e.g. Excalidraw or Office exports) are
flattened; raster images pass through untouched.

- `-o, --output PATH` — output path (default: `<master>.flat.svg`).

### `svgpages pages MASTER.svg`

List the document's pages — index, physical size in mm and inches, and
`inkscape:label` — without rendering anything.

### `svgpages check MASTER.svg`

Preflight without rendering: broken links (recursing into linked SVGs),
absolute paths, rasters below the resolution floor at their placed size,
fonts referenced but not installed, and `<use>` elements Inkscape 1.2
won't render (plain `href` without `xlink:href`).

- `--min-dpi DPI` — resolution floor for raster warnings (default: 300).

### `svgpages verify OUTPUT.pdf`

Report raster image XObjects per page of an existing PDF ("did anything
rasterize?"). Rasters may be legitimate (micrographs, gels); exits `2`
when any are present so scripts can notice.

Exit codes: `0` clean, `1` hard failure, `2` completed with warnings —
so a Makefile target can gate on
`svgpages check assembly.svg && svgpages export assembly.svg out.pdf`.

## Requirements

- Python ≥ 3.10 (`lxml`, `pikepdf`)
- Inkscape ≥ 1.2 (Pages support; multi-page PDF export)

Fonts are resolved by whichever Inkscape does the rendering. Under WSL,
documents authored on Windows (e.g. using Arial) should be exported with the
Windows Inkscape — `--inkscape "/mnt/c/Program Files/Inkscape/bin/inkscape.exe"`
or `SVGPAGES_INKSCAPE` — so the real fonts get embedded; a Linux Inkscape
without the font silently substitutes (metric-compatible, but the text ends
up as outlines of the substitute). Both `check` and `export` detect this:
when a referenced font isn't installed locally they warn (exit `2`) and, if
the Windows binary is reachable, print the exact flag to use.

## Install

```console
pip install -e .          # from a checkout
pip install -e '.[test]' && pytest   # with tests
```

## Status

Milestones 1–2 complete (flatten engine, Inkscape + Chrome backends,
`pages`/`check`/`verify`, page selection, labels/metadata), plus data-URI
SVG flattening and WSL font-substitution warnings from M3. Remaining
(M3): `--per-page-files`, in-place flatten refresh via the recorded
`svgpages:src` provenance, tagged release — see [PLAN.md](PLAN.md).
