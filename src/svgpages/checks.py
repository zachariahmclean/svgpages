"""Preflight checks (no rendering) and PDF raster verification."""
from __future__ import annotations

import base64
import math
import re
import shutil
import struct
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf
from lxml import etree

from .pages import SVG_NS, XLINK_NS, document_scale

_IMAGE_TAG = f"{{{SVG_NS}}}image"
_USE_TAG = f"{{{SVG_NS}}}use"
_STYLE_TAG = f"{{{SVG_NS}}}style"
_XLINK_HREF = f"{{{XLINK_NS}}}href"

_FONT_FAMILY_RE = re.compile(r"font-family\s*:\s*([^;}]+)")

_GENERIC_FONTS = {
    "serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui",
    "ui-serif", "ui-sans-serif", "ui-monospace", "ui-rounded", "math",
    "emoji", "fangsong", "inherit", "initial", "unset", "none",
}

_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")


@dataclass
class CheckReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.errors:
            return 1
        if self.warnings:
            return 2
        return 0


def check_svg(master: Path | str, min_dpi: float = 300.0) -> CheckReport:
    """Preflight a master SVG: link resolution (recursive through linked
    SVGs), absolute-path portability, raster resolution at placed size
    (master level only), Inkscape <use href> quirk, referenced vs
    installed fonts."""
    report = CheckReport()
    master = Path(master).resolve()
    tree = etree.parse(str(master))
    fonts: set[str] = set()
    seen: set[Path] = set()
    _walk(tree.getroot(), master.parent, master.name, report, fonts, seen,
          min_dpi, scale=document_scale(tree.getroot()))
    _check_fonts_installed(fonts, report)
    return report


def _walk(root: etree._Element, base_dir: Path, docname: str,
          report: CheckReport, fonts: set[str], seen: set[Path],
          min_dpi: float, scale: float | None) -> None:
    fonts |= referenced_font_families(root)

    uses_by_id: dict[str, list] = {}
    for use in root.iter(_USE_TAG):
        ref = use.get("href") or use.get(_XLINK_HREF)
        if ref and ref.startswith("#"):
            uses_by_id.setdefault(ref[1:], []).append(use)

    n_plain_use = sum(
        1 for el in root.iter(_USE_TAG)
        if el.get("href") and not el.get(_XLINK_HREF)
    )
    if n_plain_use:
        report.warnings.append(
            f"{docname}: {n_plain_use} <use> element(s) with plain 'href' "
            "but no xlink:href — Inkscape 1.2 does not render these"
        )

    for image in root.iter(_IMAGE_TAG):
        href = image.get("href") or image.get(_XLINK_HREF)
        if not href:
            continue
        if href.startswith("data:"):
            if scale is not None:
                _check_raster_dpi(image, _data_uri_bytes(href), "embedded raster",
                                  docname, report, min_dpi, scale, uses_by_id)
            continue
        parsed = urllib.parse.urlparse(href)
        if parsed.scheme and len(parsed.scheme) > 1 and parsed.scheme != "file":
            report.warnings.append(
                f"{docname}: remote href will not resolve offline: {href}"
            )
            continue
        path_part = urllib.parse.unquote(parsed.path or href)
        if Path(path_part).is_absolute() or _WIN_DRIVE_RE.match(path_part):
            report.warnings.append(
                f"{docname}: absolute path breaks portability: {href}"
            )
            target = Path(path_part)
        else:
            target = (base_dir / path_part).resolve()
        if not target.exists():
            report.errors.append(f"{docname}: linked file not found: {href}")
            continue
        if target.suffix.lower() == ".svg":
            if target in seen:
                continue
            seen.add(target)
            try:
                child = etree.parse(str(target))
            except etree.LxmlError as e:
                report.errors.append(f"{docname}: cannot parse linked SVG {href}: {e}")
                continue
            _walk(child.getroot(), target.parent, target.name, report, fonts,
                  seen, min_dpi, scale=None)
        elif scale is not None:
            _check_raster_dpi(image, target.read_bytes(), href, docname,
                              report, min_dpi, scale, uses_by_id)


_TRANSFORM_RE = re.compile(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)")

_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mat_mult(m: tuple, n: tuple) -> tuple:
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1,
    )


def parse_transform(s: str) -> tuple:
    """Parse an SVG transform attribute into an (a,b,c,d,e,f) matrix."""
    m = _IDENTITY
    for func, argstr in _TRANSFORM_RE.findall(s):
        args = [float(v) for v in re.split(r"[\s,]+", argstr.strip()) if v]
        if func == "matrix" and len(args) == 6:
            n = tuple(args)
        elif func == "translate":
            tx, ty = args[0], args[1] if len(args) > 1 else 0.0
            n = (1, 0, 0, 1, tx, ty)
        elif func == "scale":
            sx, sy = args[0], args[1] if len(args) > 1 else args[0]
            n = (sx, 0, 0, sy, 0, 0)
        elif func == "rotate":
            r = math.radians(args[0])
            n = (math.cos(r), math.sin(r), -math.sin(r), math.cos(r), 0, 0)
            if len(args) == 3:
                cx, cy = args[1], args[2]
                n = _mat_mult(_mat_mult((1, 0, 0, 1, cx, cy), n), (1, 0, 0, 1, -cx, -cy))
        elif func == "skewX":
            n = (1, 0, math.tan(math.radians(args[0])), 1, 0, 0)
        elif func == "skewY":
            n = (1, math.tan(math.radians(args[0])), 0, 1, 0, 0)
        else:
            continue
        m = _mat_mult(m, n)
    return m


def _scale_of(m: tuple) -> tuple[float, float]:
    a, b, c, d, _, _ = m
    return math.hypot(a, b), math.hypot(c, d)


def _cumulative_scale(el: etree._Element) -> tuple[float, float]:
    """Effective (sx, sy) scale from all transform attributes on the
    element and its ancestors."""
    m = _IDENTITY
    chain = []
    node = el
    while node is not None:
        t = node.get("transform")
        if t:
            chain.append(t)
        node = node.getparent()
    for t in reversed(chain):
        m = _mat_mult(m, parse_transform(t))
    return _scale_of(m)


def _under_defs(el: etree._Element) -> bool:
    node = el.getparent()
    while node is not None:
        if isinstance(node.tag, str) and node.tag in (
            f"{{{SVG_NS}}}defs", f"{{{SVG_NS}}}symbol"
        ):
            return True
        node = node.getparent()
    return False


def _placement_scales(image: etree._Element,
                      uses_by_id: dict[str, list]) -> list[tuple[float, float]]:
    """Every (sx, sy) at which this image is actually rendered: directly
    (unless it lives in <defs>/<symbol>) and via <use> references
    (Office-style SVG places images in defs and scales at the use site)."""
    placements: list[tuple[float, float]] = []
    if not _under_defs(image):
        placements.append(_cumulative_scale(image))
    img_id = image.get("id")
    if img_id and img_id in uses_by_id:
        own = image.get("transform")
        osx, osy = _scale_of(parse_transform(own)) if own else (1.0, 1.0)
        for use in uses_by_id[img_id]:
            usx, usy = _cumulative_scale(use)
            placements.append((usx * osx, usy * osy))
    return placements


def _check_raster_dpi(image: etree._Element, data: bytes | None, what: str,
                      docname: str, report: CheckReport, min_dpi: float,
                      scale: float, uses_by_id: dict[str, list]) -> None:
    """Warn when a raster's effective resolution at its largest placed
    size is below min_dpi. Accounts for element/ancestor transforms and
    <use> instantiation."""
    if data is None:
        return
    dims = image_dimensions(data)
    if dims is None:
        return
    px_w, px_h = dims
    placements = _placement_scales(image, uses_by_id)
    if not placements:
        return  # defined but never rendered
    sx = max(s for s, _ in placements)
    sy = max(s for _, s in placements)
    try:
        w_in = float(image.get("width")) * sx * scale
        h_in = float(image.get("height")) * sy * scale
    except (TypeError, ValueError):
        return
    if w_in <= 0 or h_in <= 0:
        return
    dpi = min(px_w / w_in, px_h / h_in)
    if dpi < min_dpi:
        report.warnings.append(
            f"{docname}: {what}: ~{dpi:.0f} dpi at placed size "
            f"({px_w}x{px_h}px over {w_in:.2f}x{h_in:.2f}in), below {min_dpi:.0f} dpi"
        )


def _data_uri_bytes(href: str, max_bytes: int = 4096) -> bytes | None:
    """Decode enough of a base64 data: URI to read the image header."""
    m = re.match(r"data:[^;,]+;base64,", href)
    if not m:
        return None
    payload = href[m.end():m.end() + max_bytes]
    payload = payload[: len(payload) - len(payload) % 4]
    try:
        return base64.b64decode(payload)
    except ValueError:
        return None


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Pixel dimensions of PNG or JPEG bytes (header parse, no deps)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    if data[:2] == b"\xff\xd8":  # JPEG
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + length
    return None


def referenced_font_families(root: etree._Element) -> set[str]:
    families: set[str] = set()

    def add(spec: str) -> None:
        for fam in spec.split(","):
            fam = fam.strip().strip("'\"").strip()
            if fam and fam.lower() not in _GENERIC_FONTS:
                families.add(fam)

    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if el.get("font-family"):
            add(el.get("font-family"))
        style = el.get("style")
        if style:
            for m in _FONT_FAMILY_RE.finditer(style):
                add(m.group(1))
        if el.tag == _STYLE_TAG and el.text:
            for m in _FONT_FAMILY_RE.finditer(el.text):
                add(m.group(1))
    return families


def installed_font_families() -> set[str] | None:
    """Lowercased set of locally installed font family names via
    fontconfig, or None if fc-list is unavailable."""
    if not shutil.which("fc-list"):
        return None
    try:
        result = subprocess.run(
            ["fc-list", "--format", "%{family}\n"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    installed: set[str] = set()
    for line in result.stdout.splitlines():
        for fam in line.split(","):
            fam = fam.strip()
            if fam:
                installed.add(fam.lower())
    return installed


def _check_fonts_installed(fonts: set[str], report: CheckReport) -> None:
    if not fonts:
        return
    installed = installed_font_families()
    if installed is None:
        report.infos.append(
            "fonts referenced: " + ", ".join(sorted(fonts))
            + " (fc-list unavailable; cannot check installation)"
        )
        return
    missing = sorted(f for f in fonts if f.lower() not in installed)
    if missing:
        from .render import find_windows_tool

        win = find_windows_tool("inkscape")
        hint = (
            f' — e.g. --inkscape "{win}"' if win else
            " (--inkscape/--chrome or SVGPAGES_INKSCAPE/SVGPAGES_CHROME)"
        )
        report.warnings.append(
            "fonts referenced but not installed locally: "
            + ", ".join(missing)
            + " — rendering will substitute; if the document was authored "
            "on Windows, export with the Windows Inkscape/Chrome" + hint
        )
    else:
        report.infos.append(
            f"all {len(fonts)} referenced font(s) installed: "
            + ", ".join(sorted(fonts))
        )


def count_expected_rasters(root: etree._Element) -> int:
    """Number of <image> references that are *legitimately* raster (i.e.
    not SVG): the baseline for the post-export verify. SVG-href images are
    counted as zero because they are supposed to stay vector — if a
    renderer rasterizes them, the PDF raster count exceeds this."""
    n = 0
    for image in root.iter(_IMAGE_TAG):
        href = image.get("href") or image.get(_XLINK_HREF)
        if not href:
            continue
        if href.startswith("data:"):
            if "image/svg" not in href[:64]:
                n += 1
            continue
        path = urllib.parse.urlparse(href).path or href
        if not path.lower().endswith(".svg"):
            n += 1
    return n


def inspect_pdf_rasters(pdf_path: Path | str) -> list[list[tuple[int, int]]]:
    """Per page, the pixel dimensions of every raster image XObject
    (recursing through Form XObjects)."""
    results: list[list[tuple[int, int]]] = []
    with pikepdf.open(pdf_path) as pdf:
        for page in pdf.pages:
            images: list[tuple[int, int]] = []
            _walk_xobjects(page.get("/Resources"), images, set(), 0)
            results.append(images)
    return results


def _walk_xobjects(resources, images: list[tuple[int, int]],
                   visited: set[tuple[int, int]], depth: int) -> None:
    if resources is None or depth > 10:
        return
    xobjects = resources.get("/XObject")
    if xobjects is None:
        return
    for _, obj in xobjects.items():
        objgen = obj.objgen
        if objgen != (0, 0) and objgen in visited:
            continue
        visited.add(objgen)
        subtype = obj.get("/Subtype")
        if subtype == pikepdf.Name("/Image"):
            images.append((int(obj.Width), int(obj.Height)))
        elif subtype == pikepdf.Name("/Form"):
            _walk_xobjects(obj.get("/Resources"), images, visited, depth + 1)
