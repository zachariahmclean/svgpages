import base64
import shutil
import struct
from pathlib import Path

import pytest

from svgpages.checks import check_svg, image_dimensions, referenced_font_families
from svgpages.pages import SVG_NS, XLINK_NS

from lxml import etree

FIXTURES = Path(__file__).parent / "fixtures"


def _fake_png(w: int, h: int) -> bytes:
    return (b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00"
            + b"\x00" * 16)


def _fake_jpeg(w: int, h: int) -> bytes:
    return (b"\xff\xd8"
            + b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
            + struct.pack(">HH", h, w) + b"\x00" * 12)


def _write_svg(path: Path, body: str, size='width="4in" height="4in" viewBox="0 0 4 4"') -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'<svg xmlns="{SVG_NS}" xmlns:xlink="{XLINK_NS}" {size}>{body}</svg>'
    )
    return path


def test_image_dimensions_png_jpeg():
    assert image_dimensions(_fake_png(120, 34)) == (120, 34)
    assert image_dimensions(_fake_jpeg(30, 40)) == (30, 40)
    assert image_dimensions(b"not an image") is None


def test_master_fixture_is_clean():
    report = check_svg(FIXTURES / "master.svg")
    assert report.errors == []
    assert report.warnings == []
    assert report.exit_code == 0


def test_missing_link_is_error(tmp_path):
    svg = _write_svg(tmp_path / "m.svg",
                     '<image x="0" y="0" width="1" height="1" href="gone.svg"/>')
    report = check_svg(svg)
    assert report.exit_code == 1
    assert any("gone.svg" in e for e in report.errors)


def test_absolute_path_is_warning(tmp_path):
    target = _write_svg(tmp_path / "child.svg", "")
    svg = _write_svg(tmp_path / "m.svg",
                     f'<image x="0" y="0" width="1" height="1" href="{target}"/>')
    report = check_svg(svg)
    assert report.errors == []
    assert any("absolute path" in w for w in report.warnings)


def test_low_dpi_embedded_raster_is_warning(tmp_path):
    # 100px over 2in placed size = 50 dpi
    uri = "data:image/png;base64," + base64.b64encode(_fake_png(100, 100)).decode()
    svg = _write_svg(tmp_path / "m.svg",
                     f'<image x="0" y="0" width="2" height="2" href="{uri}"/>')
    report = check_svg(svg)
    assert any("~50 dpi" in w for w in report.warnings)
    report_ok = check_svg(svg, min_dpi=40)
    assert report_ok.warnings == []


def test_raster_dpi_respects_transforms(tmp_path):
    # 300px image, width attr 300 units in a 1-unit-per-px doc, scaled to
    # a tenth by transform: 300px over ~0.31in ≈ 960 dpi — must NOT warn,
    # even though the unscaled attrs alone (300px over 3.125in ≈ 96 dpi) would
    uri = "data:image/png;base64," + base64.b64encode(_fake_png(300, 300)).decode()
    svg = _write_svg(
        tmp_path / "m.svg",
        f'<g transform="translate(5,5) scale(0.1)">'
        f'<image x="0" y="0" width="300" height="300" href="{uri}"/></g>',
        size='width="4in" height="4in" viewBox="0 0 384 384"',
    )
    report = check_svg(svg)
    assert report.warnings == []


def test_raster_dpi_via_defs_and_use(tmp_path):
    # Office-style: image in <defs> at natural px size, instantiated by a
    # <use> whose ancestor chain shrinks it. 300px placed over ~1in = 300dpi
    # -> clean; over ~2in = 150dpi -> warns.
    uri = "data:image/png;base64," + base64.b64encode(_fake_png(300, 300)).decode()

    def doc(shrink):
        return (
            f'<defs><image id="im" width="300" height="300" href="{uri}"/></defs>'
            f'<g transform="scale({shrink})"><use xlink:href="#im" transform="scale(100)"/></g>'
        )

    svg = _write_svg(tmp_path / "ok.svg", doc(96 / 30000),
                     size='width="4in" height="4in" viewBox="0 0 384 384"')
    assert check_svg(svg).warnings == []
    svg2 = _write_svg(tmp_path / "low.svg", doc(2 * 96 / 30000),
                      size='width="4in" height="4in" viewBox="0 0 384 384"')
    assert any("dpi" in w for w in check_svg(svg2).warnings)


def test_unused_defs_image_not_checked(tmp_path):
    uri = "data:image/png;base64," + base64.b64encode(_fake_png(10, 10)).decode()
    svg = _write_svg(tmp_path / "m.svg",
                     f'<defs><image id="never" width="10" height="10" href="{uri}"/></defs>')
    assert check_svg(svg).warnings == []


def test_low_dpi_linked_raster_is_warning(tmp_path):
    (tmp_path / "photo.png").write_bytes(_fake_png(100, 100))
    svg = _write_svg(tmp_path / "m.svg",
                     '<image x="0" y="0" width="2" height="2" href="photo.png"/>')
    report = check_svg(svg)
    assert any("photo.png" in w and "dpi" in w for w in report.warnings)


def test_plain_use_href_is_warning(tmp_path):
    svg = _write_svg(tmp_path / "m.svg",
                     '<circle id="c" r="1"/><use href="#c" x="2"/>')
    report = check_svg(svg)
    assert any("xlink:href" in w for w in report.warnings)


def test_check_recurses_into_linked_svgs(tmp_path):
    _write_svg(tmp_path / "child.svg",
               '<image x="0" y="0" width="1" height="1" href="missing.png"/>')
    svg = _write_svg(tmp_path / "m.svg",
                     '<image x="0" y="0" width="1" height="1" href="child.svg"/>')
    report = check_svg(svg)
    assert any("missing.png" in e for e in report.errors)


def test_font_families_collected():
    root = etree.fromstring(
        f'<svg xmlns="{SVG_NS}">'
        '<text font-family="Arial, sans-serif">a</text>'
        "<text style=\"font-family: 'Times New Roman'; fill: red\">b</text>"
        "<style>.t { font-family: Helvetica, monospace; }</style>"
        "</svg>".encode()
    )
    assert referenced_font_families(root) == {"Arial", "Times New Roman", "Helvetica"}


@pytest.mark.skipif(shutil.which("fc-list") is None, reason="fc-list not available")
def test_missing_font_is_warning(tmp_path):
    svg = _write_svg(tmp_path / "m.svg",
                     '<text font-family="DefinitelyNotARealFont123">x</text>')
    report = check_svg(svg)
    assert any("DefinitelyNotARealFont123" in w for w in report.warnings)
