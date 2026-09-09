"""Flatten linked SVG <image> references into inline vector content.

The core engine: replaces each ``<image href="child.svg">`` with the child
document inlined as a nested ``<svg>`` carrying the image's placement
geometry. Nested-svg + the child's viewBox reproduces <image> scaling and
clipping semantics, but the content stays true vector through any PDF
export. Every inlined child gets its ids (and, when stylesheets are
present, class names) namespaced to avoid collisions — sibling figures from
the same plotting pipeline routinely share ids.
"""
from __future__ import annotations

import copy
import itertools
import os
import re
import urllib.parse
from pathlib import Path

from lxml import etree

from .pages import INKSCAPE_NS, PX_PER_IN, SODIPODI_NS, SVG_NS, XLINK_NS, length_to_inches

SVGPAGES_NS = "urn:svgpages"

_IMAGE_TAG = f"{{{SVG_NS}}}image"
_STYLE_TAG = f"{{{SVG_NS}}}style"
_XLINK_HREF = f"{{{XLINK_NS}}}href"

_URL_REF_RE = re.compile(r"""url\(\s*['"]?#([^)'"\s]+)['"]?\s*\)""")

# <image> attributes that transfer onto the nested <svg> itself
_GEOMETRY_ATTRS = ("x", "y", "width", "height", "preserveAspectRatio")
# child-root attributes that must not override the placement geometry
_ROOT_ATTR_DENYLIST = {"width", "height", "viewBox", "x", "y", "version", "preserveAspectRatio"}


class FlattenError(Exception):
    """A linked SVG could not be flattened (missing file, circular link…)."""


def flatten_file(master: Path | str, max_depth: int = 10) -> tuple[etree._ElementTree, list[str]]:
    """Parse ``master`` and flatten all linked SVGs, recursively.

    Returns the modified tree and a list of warning strings. Raises
    FlattenError on missing linked files, circular links, or over-deep
    nesting.
    """
    master = Path(master).resolve()
    tree = etree.parse(str(master))
    warnings: list[str] = []
    counter = itertools.count(1)
    _flatten_tree(tree, master.parent, (master,), warnings, counter, max_depth)
    return tree, warnings


def write_flattened(tree: etree._ElementTree, out_path: Path | str, master_dir: Path | str) -> None:
    """Serialize a flattened tree; if it lands outside the master's
    directory, rebase remaining relative hrefs (rasters) so they still
    resolve from the new location."""
    out_path = Path(out_path).resolve()
    master_dir = Path(master_dir).resolve()
    if out_path.parent != master_dir:
        # rebase a copy: the caller may write the same tree to several
        # locations, and rebasing twice would corrupt the hrefs
        tree = copy.deepcopy(tree)
        _rebase_relative_hrefs(tree, master_dir, out_path.parent)
    out_path.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))


def _flatten_tree(tree, base_dir: Path, stack: tuple[Path, ...],
                  warnings: list[str], counter, max_depth: int) -> None:
    for image in list(tree.getroot().iter(_IMAGE_TAG)):
        href = image.get("href") or image.get(_XLINK_HREF)
        if not href:
            continue
        target = _resolve_svg_href(href, base_dir, warnings)
        if target is None:
            continue  # raster / data: / remote / fragment — left as-is
        if not target.exists():
            raise FlattenError(f"linked SVG not found: {href!r} (resolved to {target})")
        if target in stack:
            chain = " -> ".join(p.name for p in (*stack, target))
            raise FlattenError(f"circular link: {chain}")
        if len(stack) > max_depth:
            raise FlattenError(f"links nested deeper than {max_depth} at {target}")

        child = etree.parse(str(target))
        _flatten_tree(child, target.parent, (*stack, target), warnings, counter, max_depth)
        prefix = _make_prefix(target, counter)
        _namespace_child(child.getroot(), prefix)
        replacement = _inline(image, child.getroot(), href, warnings)
        image.getparent().replace(image, replacement)


def _resolve_svg_href(href: str, base_dir: Path, warnings: list[str]) -> Path | None:
    """Return the local path of an SVG href, or None if it isn't a
    flattenable local SVG file."""
    if href.startswith("data:"):
        if "image/svg" in href[:64]:
            warnings.append(f"data: URI SVG left as-is (not flattened): {href[:48]}…")
        return None
    parsed = urllib.parse.urlparse(href)
    # A one-letter "scheme" is really a Windows drive letter (C:\...).
    if parsed.scheme and len(parsed.scheme) > 1 and parsed.scheme != "file":
        if parsed.path.lower().endswith(".svg"):
            warnings.append(f"remote SVG left as-is (not flattened): {href}")
        return None
    if parsed.fragment:
        warnings.append(f"href with fragment left as-is (not flattened): {href}")
        return None
    path_part = urllib.parse.unquote(parsed.path) if parsed.scheme == "file" or "%" in href else href
    if not path_part.lower().endswith(".svg"):
        return None  # raster image etc.
    p = Path(path_part)
    return p.resolve() if p.is_absolute() else (base_dir / p).resolve()


def _make_prefix(target: Path, counter) -> str:
    stem = re.sub(r"[^A-Za-z0-9_]+", "_", target.stem) or "svg"
    return f"{stem}_{next(counter)}__"


def _namespace_child(root: etree._Element, prefix: str) -> None:
    """Prefix every id in the child tree and rewrite all references to
    them; when the child carries stylesheets, prefix class names too."""
    idmap = {
        el.get("id"): prefix + el.get("id")
        for el in root.iter()
        if isinstance(el.tag, str) and el.get("id")
    }
    style_elems = [el for el in root.iter(_STYLE_TAG)]
    classmap: dict[str, str] = {}
    if style_elems:
        for el in root.iter():
            if isinstance(el.tag, str) and el.get("class"):
                for tok in el.get("class").split():
                    classmap[tok] = prefix + tok

    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for name, val in list(el.attrib.items()):
            if name == "id":
                el.set("id", idmap[val])
            elif name == "class" and classmap:
                el.set("class", " ".join(classmap.get(t, t) for t in val.split()))
            else:
                new = _rewrite_ref_value(val, idmap)
                if new != val:
                    el.set(name, new)
        if el.tag == _STYLE_TAG and el.text:
            el.text = _rewrite_css(el.text, idmap, classmap)


def _rewrite_ref_value(val: str, idmap: dict[str, str]) -> str:
    if val.startswith("#"):
        return "#" + idmap.get(val[1:], val[1:])
    return _URL_REF_RE.sub(lambda m: f"url(#{idmap.get(m.group(1), m.group(1))})", val)


def _rewrite_css(text: str, idmap: dict[str, str], classmap: dict[str, str]) -> str:
    text = _URL_REF_RE.sub(lambda m: f"url(#{idmap.get(m.group(1), m.group(1))})", text)
    # #id selectors; hex colors etc. never match because only known ids map
    text = re.sub(r"#([-\w]+)", lambda m: "#" + idmap.get(m.group(1), m.group(1)), text)
    if classmap:
        text = re.sub(r"\.([-\w]+)", lambda m: "." + classmap.get(m.group(1), m.group(1)), text)
    return text


def _inline(image: etree._Element, child_root: etree._Element, href: str,
            warnings: list[str]) -> etree._Element:
    nested = etree.Element(
        f"{{{SVG_NS}}}svg", nsmap={None: SVG_NS, "svgpages": SVGPAGES_NS}
    )
    for attr in _GEOMETRY_ATTRS:
        v = image.get(attr)
        if v is not None:
            nested.set(attr, v)
    # an <image> normally has explicit width/height; fall back to the
    # child's intrinsic size if not
    for attr in ("width", "height"):
        if nested.get(attr) is None and child_root.get(attr):
            nested.set(attr, child_root.get(attr))

    viewbox = child_root.get("viewBox") or _synthesize_viewbox(child_root, href, warnings)
    if viewbox:
        nested.set("viewBox", viewbox)
    nested.set(f"{{{SVGPAGES_NS}}}src", href)

    # carry over child-root attributes that affect rendering (style,
    # font-*, xml:space, its now-prefixed id, …), minus geometry and
    # editor-namespace cruft
    for name, val in child_root.attrib.items():
        if name in _ROOT_ATTR_DENYLIST:
            continue
        if name.startswith("{"):
            ns = name[1:].split("}", 1)[0]
            if ns in (SODIPODI_NS, INKSCAPE_NS):
                continue
        nested.set(name, val)

    # move the child's content, dropping editor-only elements
    for node in list(child_root):
        if isinstance(node.tag, str):
            if node.tag == f"{{{SODIPODI_NS}}}namedview":
                continue
            if node.tag == f"{{{SVG_NS}}}metadata":
                continue
        nested.append(node)

    # image-level attributes that apply to the placed graphic as a whole
    # (transform, id, style, opacity, clip-path, inkscape:label, …) go on a
    # wrapping <g>, since geometry lives on the nested <svg>
    extra = {
        n: v
        for n, v in image.attrib.items()
        if n not in _GEOMETRY_ATTRS and n not in ("href", _XLINK_HREF)
    }
    if extra:
        g = etree.Element(f"{{{SVG_NS}}}g")
        for n, v in extra.items():
            g.set(n, v)
        g.append(nested)
        return g
    return nested


def _synthesize_viewbox(child_root: etree._Element, href: str,
                        warnings: list[str]) -> str | None:
    w_attr, h_attr = child_root.get("width"), child_root.get("height")
    if not (w_attr and h_attr):
        warnings.append(f"{href}: no viewBox and no width/height on child root; scaling may be wrong")
        return None
    try:
        w_px = length_to_inches(w_attr) * PX_PER_IN
        h_px = length_to_inches(h_attr) * PX_PER_IN
    except ValueError:
        warnings.append(f"{href}: could not parse child width/height ({w_attr!r}, {h_attr!r})")
        return None
    return f"0 0 {w_px:g} {h_px:g}"


def _rebase_relative_hrefs(tree, master_dir: Path, out_dir: Path) -> None:
    for image in tree.getroot().iter(_IMAGE_TAG):
        for attr in ("href", _XLINK_HREF):
            href = image.get(attr)
            if not href or href.startswith(("data:", "#")):
                continue
            parsed = urllib.parse.urlparse(href)
            if parsed.scheme or parsed.fragment:
                continue
            path_part = urllib.parse.unquote(href) if "%" in href else href
            if Path(path_part).is_absolute():
                continue
            resolved = (master_dir / path_part).resolve()
            rel = os.path.relpath(resolved, out_dir).replace(os.sep, "/")
            image.set(attr, urllib.parse.quote(rel, safe="/"))
