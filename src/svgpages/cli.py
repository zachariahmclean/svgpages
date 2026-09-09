"""Command-line interface. Thin: argument parsing and orchestration only."""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from lxml import etree

from . import __version__
from .checks import (
    check_svg,
    count_expected_rasters,
    inspect_pdf_rasters,
    installed_font_families,
    referenced_font_families,
)
from .flatten import FlattenError, flatten_file, write_flattened
from .merge import finalize_pdf, merge_page_pdfs
from .pages import crop_to_page, get_pages, parse_page_spec
from .render import (
    RenderError,
    export_pdf_inkscape,
    find_chrome,
    find_inkscape,
    find_windows_tool,
    print_page_pdf_chrome,
    write_page_html,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_WARNINGS = 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="svgpages",
        description="Vector-true multi-page PDF export for Inkscape Pages "
                    "documents with linked SVG figures.",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    ex = sub.add_parser(
        "export", help="flatten and render a master SVG to a multi-page vector PDF"
    )
    ex.add_argument("master_svg", type=Path)
    ex.add_argument("output_pdf", type=Path)
    ex.add_argument("--backend", choices=["inkscape", "chrome"], default="inkscape")
    ex.add_argument("--pages", default=None, metavar="SPEC",
                    help="subset of pages to export, e.g. '1,3-5'")
    ex.add_argument("--inkscape", default=None, help="path to the Inkscape executable")
    ex.add_argument("--chrome", default=None, help="path to the Chrome/Chromium executable")
    ex.add_argument("--flat-svg", type=Path, default=None, metavar="PATH",
                    help="where to write the flattened SVG that is exported "
                         "alongside the PDF (default: the output PDF with a "
                         ".flat.svg suffix); it can be hand-adjusted and "
                         "re-exported")
    ex.add_argument("--no-flat-svg", action="store_true",
                    help="do not write the flattened SVG next to the PDF")
    ex.add_argument("--no-flatten", action="store_true",
                    help="render with linked images intact (they will rasterize)")
    ex.add_argument("--no-verify", action="store_true",
                    help="skip the post-export raster check of the PDF")
    ex.add_argument("--keep-temp", action="store_true",
                    help="keep intermediate files for debugging")

    fl = sub.add_parser(
        "flatten", help="write a self-contained SVG with linked SVGs inlined as vectors"
    )
    fl.add_argument("master_svg", type=Path)
    fl.add_argument("-o", "--output", type=Path, default=None,
                    help="output path (default: <master>.flat.svg)")

    pg = sub.add_parser("pages", help="list the document's pages (dry run)")
    pg.add_argument("master_svg", type=Path)

    ck = sub.add_parser(
        "check", help="preflight: links, absolute paths, raster dpi, fonts"
    )
    ck.add_argument("master_svg", type=Path)
    ck.add_argument("--min-dpi", type=float, default=300.0,
                    help="warn on rasters below this effective dpi (default 300)")

    vf = sub.add_parser(
        "verify", help="report raster images per page of an existing PDF"
    )
    vf.add_argument("pdf", type=Path)

    args = ap.parse_args(argv)
    try:
        handler = {
            "export": _cmd_export,
            "flatten": _cmd_flatten,
            "pages": _cmd_pages,
            "check": _cmd_check,
            "verify": _cmd_verify,
        }[args.command]
        return handler(args)
    except (FlattenError, RenderError, OSError, etree.LxmlError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR


def _report_warnings(warnings: list[str]) -> int:
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return EXIT_WARNINGS if warnings else EXIT_OK


def _require_input(path: Path) -> Path | None:
    resolved = path.resolve()
    if not resolved.exists():
        print(f"error: {resolved} does not exist", file=sys.stderr)
        return None
    return resolved


def _cmd_export(args: argparse.Namespace) -> int:
    master = _require_input(args.master_svg)
    if master is None:
        return EXIT_ERROR

    pages, _ = get_pages(master)
    keep = None
    if args.pages:
        try:
            keep = parse_page_spec(args.pages, len(pages))
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return EXIT_ERROR
    selected = [p for p in pages if keep is None or p.index in keep]

    print(f"{len(pages)} page(s) in {master.name}"
          + (f", exporting {len(selected)}" if keep else "") + ":")
    for p in selected:
        label = f" ({p.label})" if p.label else ""
        print(f"  {p.id}{label}: {p.w_mm:.1f} x {p.h_mm:.1f} mm")

    if args.backend == "inkscape":
        tool = find_inkscape(args.inkscape)
        if not tool:
            print("error: could not find Inkscape (>= 1.2 required). Install "
                  "it, pass --inkscape PATH, or set SVGPAGES_INKSCAPE.",
                  file=sys.stderr)
            return EXIT_ERROR
    else:
        tool = find_chrome(args.chrome)
        if not tool:
            print("error: could not find Chrome/Chromium. Install it, pass "
                  "--chrome PATH, or set SVGPAGES_CHROME.", file=sys.stderr)
            return EXIT_ERROR

    warnings: list[str] = []
    out_pdf = Path(args.output_pdf).resolve()
    title = master.stem
    labels = [p.label for p in selected]
    tempdir = Path(tempfile.mkdtemp(prefix=".svgpages_", dir=master.parent))
    try:
        if args.no_flatten:
            tree = etree.parse(str(master))
        else:
            tree, warnings = flatten_file(master)
        # written (with rebased raster hrefs) even for --no-flatten, so
        # both backends can render from the tempdir with links resolving
        render_src = tempdir / f"{master.stem}_flat.svg"
        write_flattened(tree, render_src, master.parent)

        if not args.no_flatten and not args.no_flat_svg:
            flat_out = args.flat_svg or out_pdf.with_name(out_pdf.stem + ".flat.svg")
            if Path(flat_out).resolve() == master:
                warnings.append(
                    f"not overwriting the input with the flattened SVG: {flat_out}"
                )
            else:
                write_flattened(tree, flat_out, master.parent)
                print(f"wrote {flat_out}")

        _warn_missing_fonts(tree, tool, args.backend, warnings)

        if args.backend == "inkscape":
            raw_pdf = tempdir / "render.pdf"
            export_pdf_inkscape(tool, render_src, raw_pdf)
            finalize_pdf(raw_pdf, out_pdf, keep, labels, title)
        else:
            src_tree = etree.parse(str(render_src))
            page_pdfs = []
            for p in selected:
                cropped = crop_to_page(src_tree, p)
                html = tempdir / f"{p.id}.html"
                page_pdf = tempdir / f"{p.id}.pdf"
                write_page_html(cropped, p, html)
                print_page_pdf_chrome(tool, html, page_pdf)
                page_pdfs.append(page_pdf)
            merge_page_pdfs(page_pdfs, out_pdf, labels, title, warnings)
        print(f"wrote {out_pdf}")

        if not args.no_verify:
            expected = count_expected_rasters(tree.getroot())
            actual = sum(len(imgs) for imgs in inspect_pdf_rasters(out_pdf))
            if actual > expected:
                warnings.append(
                    f"verify: PDF contains {actual} raster image(s) but the "
                    f"source references only {expected} — something may have "
                    "been rasterized (run 'svgpages verify' for details)"
                )
            else:
                print(f"verify: {actual} raster image(s) in the PDF, "
                      f"{expected} raster reference(s) in the source — OK")
    finally:
        if args.keep_temp:
            print(f"(kept intermediate files in {tempdir})")
        else:
            shutil.rmtree(tempdir, ignore_errors=True)
    return _report_warnings(warnings)


def _warn_missing_fonts(tree, tool: str, backend: str, warnings: list[str]) -> None:
    """Before rendering with a local (non-Windows) tool, warn when the
    document references fonts fontconfig doesn't know — they will be
    silently substituted (and end up as outlines of the substitute)."""
    if tool.lower().endswith(".exe"):
        return  # Windows binary resolves fonts from the Windows store
    fonts = referenced_font_families(tree.getroot())
    if not fonts:
        return
    installed = installed_font_families()
    if installed is None:
        return
    missing = sorted(f for f in fonts if f.lower() not in installed)
    if not missing:
        return
    win = find_windows_tool(backend)
    hint = (
        f' — the Windows binary likely has them: --{backend} "{win}" '
        f"or SVGPAGES_{backend.upper()}" if win else ""
    )
    warnings.append(
        "fonts not installed locally will be substituted: "
        + ", ".join(missing) + hint
    )


def _cmd_flatten(args: argparse.Namespace) -> int:
    master = _require_input(args.master_svg)
    if master is None:
        return EXIT_ERROR
    out = args.output or master.with_name(master.stem + ".flat.svg")
    if Path(out).resolve() == master:
        print("error: output would overwrite the master SVG", file=sys.stderr)
        return EXIT_ERROR
    tree, warnings = flatten_file(master)
    write_flattened(tree, out, master.parent)
    print(f"wrote {out}")
    return _report_warnings(warnings)


def _cmd_pages(args: argparse.Namespace) -> int:
    master = _require_input(args.master_svg)
    if master is None:
        return EXIT_ERROR
    pages, _ = get_pages(master)
    print(f"{len(pages)} page(s) in {master.name}:")
    for p in pages:
        label = p.label or ""
        print(f"  {p.index:>3}  {p.w_mm:7.1f} x {p.h_mm:7.1f} mm  "
              f"({p.w_in:5.2f} x {p.h_in:5.2f} in)  {label}")
    return EXIT_OK


def _cmd_check(args: argparse.Namespace) -> int:
    master = _require_input(args.master_svg)
    if master is None:
        return EXIT_ERROR
    report = check_svg(master, min_dpi=args.min_dpi)
    for e in report.errors:
        print(f"error: {e}")
    for w in report.warnings:
        print(f"warning: {w}")
    for i in report.infos:
        print(i)
    if report.exit_code == EXIT_OK:
        print("check: OK")
    return report.exit_code


def _cmd_verify(args: argparse.Namespace) -> int:
    pdf = _require_input(args.pdf)
    if pdf is None:
        return EXIT_ERROR
    per_page = inspect_pdf_rasters(pdf)
    total = 0
    for i, images in enumerate(per_page, 1):
        total += len(images)
        if images:
            dims = ", ".join(f"{w}x{h}px" for w, h in images)
            print(f"page {i}: {len(images)} raster image(s): {dims}")
        else:
            print(f"page {i}: fully vector")
    print(f"total: {total} raster image(s) across {len(per_page)} page(s)"
          + (" — rasters may be legitimate (micrographs, gels, …)" if total else ""))
    return EXIT_WARNINGS if total else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
