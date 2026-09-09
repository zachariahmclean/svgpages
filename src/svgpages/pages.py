"""Parse Inkscape Pages geometry and physical units from a master SVG.

Pure logic: no rendering, no subprocesses. Everything here is unit-testable.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
# NB: "sodipodi-0.dtd", not "sodipodi-0.0.dtd" — with the wrong URI Inkscape
# silently ignores the namedview and its pages.
SODIPODI_NS = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
XLINK_NS = "http://www.w3.org/1999/xlink"

PX_PER_IN = 96.0  # CSS / Inkscape 1.x user-unit scale

_UNIT_TO_IN = {
    "mm": 1 / 25.4,
    "cm": 1 / 2.54,
    "in": 1.0,
    "pt": 1 / 72.0,
    "pc": 1 / 6.0,
}


def length_to_inches(value: str, px_per_in: float = PX_PER_IN) -> float:
    """Parse an SVG length ('210mm', '8.5in', '800') into inches.

    Bare numbers and 'px' are user units at px_per_in.
    """
    value = value.strip()
    for unit, factor in _UNIT_TO_IN.items():
        if value.endswith(unit):
            return float(value[: -len(unit)]) * factor
    if value.endswith("px"):
        return float(value[:-2]) / px_per_in
    return float(value) / px_per_in


@dataclass(frozen=True)
class Page:
    index: int  # 1-based
    label: str | None  # inkscape:label, if any
    # geometry in the master document's user units
    x: float
    y: float
    w: float
    h: float
    # physical size
    w_in: float
    h_in: float

    @property
    def id(self) -> str:
        return f"page{self.index:02d}"

    @property
    def w_mm(self) -> float:
        return self.w_in * 25.4

    @property
    def h_mm(self) -> float:
        return self.h_in * 25.4


def document_scale(root: etree._Element) -> float:
    """Inches per user unit, from the document width vs viewBox width."""
    scale = 1.0 / PX_PER_IN
    viewbox = root.get("viewBox")
    width_attr = root.get("width")
    if viewbox and width_attr:
        try:
            vb_w = float(viewbox.split()[2])
            if vb_w:
                scale = length_to_inches(width_attr) / vb_w
        except (ValueError, IndexError):
            pass
    return scale


def parse_pages(tree: etree._ElementTree) -> list[Page]:
    """Return the document's Inkscape pages; whole doc as one page if none."""
    root = tree.getroot()
    scale = document_scale(root)

    pages = []
    for i, p in enumerate(root.findall(f".//{{{INKSCAPE_NS}}}page"), start=1):
        x, y = float(p.get("x", 0)), float(p.get("y", 0))
        w, h = float(p.get("width")), float(p.get("height"))
        label = p.get(f"{{{INKSCAPE_NS}}}label")
        pages.append(Page(i, label, x, y, w, h, w * scale, h * scale))
    if pages:
        return pages

    # No explicit Inkscape Pages — treat the whole document as one page.
    viewbox = root.get("viewBox")
    if viewbox:
        x, y, w, h = (float(v) for v in viewbox.split())
    else:
        x = y = 0.0
        w_attr, h_attr = root.get("width"), root.get("height")
        w = length_to_inches(w_attr) * PX_PER_IN if w_attr else 800.0
        h = length_to_inches(h_attr) * PX_PER_IN if h_attr else 600.0
    return [Page(1, None, x, y, w, h, w * scale, h * scale)]


def get_pages(svg_path: Path | str) -> tuple[list[Page], etree._ElementTree]:
    tree = etree.parse(str(svg_path))
    return parse_pages(tree), tree


def parse_page_spec(spec: str, n_pages: int) -> list[int]:
    """Parse a page-subset spec like '1,3-5' into sorted unique 1-based
    indices, validating against the document's page count."""
    indices: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        try:
            if "-" in part:
                a_s, b_s = part.split("-", 1)
                a, b = int(a_s), int(b_s)
                if a > b:
                    raise ValueError
                indices.update(range(a, b + 1))
            else:
                indices.add(int(part))
        except ValueError:
            raise ValueError(f"invalid page spec {spec!r} (bad part: {part!r})") from None
    out_of_range = sorted(i for i in indices if i < 1 or i > n_pages)
    if out_of_range:
        raise ValueError(
            f"page(s) {out_of_range} out of range: document has {n_pages} page(s)"
        )
    if not indices:
        raise ValueError(f"empty page spec {spec!r}")
    return sorted(indices)


def crop_to_page(tree: etree._ElementTree, page: Page) -> etree._Element:
    """Return a copy of the document root cropped to one page's region,
    with width/height in real physical units (inches)."""
    root = copy.deepcopy(tree.getroot())
    root.set("viewBox", f"{page.x} {page.y} {page.w} {page.h}")
    root.set("width", f"{page.w_in}in")
    root.set("height", f"{page.h_in}in")
    return root
