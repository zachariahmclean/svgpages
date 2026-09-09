"""Integration tests: real Inkscape / Chrome exports of the fixture master.

Backend-specific tests are skipped when the corresponding tool is missing.
"""
import shutil
from pathlib import Path

import pikepdf
import pytest

from svgpages.cli import main
from svgpages.render import find_chrome, find_inkscape

FIXTURES = Path(__file__).parent / "fixtures"
MASTER = str(FIXTURES / "master.svg")

needs_inkscape = pytest.mark.skipif(find_inkscape() is None, reason="Inkscape not found")
needs_chrome = pytest.mark.skipif(find_chrome() is None, reason="Chrome not found")


def _count_raster_images(pdf: pikepdf.Pdf) -> int:
    def walk(resources) -> int:
        n = 0
        xobjects = resources.get("/XObject") if resources is not None else None
        if xobjects is None:
            return 0
        for _, obj in xobjects.items():
            subtype = obj.get("/Subtype")
            if subtype == pikepdf.Name("/Image"):
                n += 1
            elif subtype == pikepdf.Name("/Form"):
                n += walk(obj.get("/Resources"))
        return n

    return sum(walk(page.get("/Resources")) for page in pdf.pages)


def _page_sizes_mm(pdf: pikepdf.Pdf) -> list[tuple[float, float]]:
    sizes = []
    for page in pdf.pages:
        box = [float(v) for v in page.MediaBox]
        sizes.append(
            ((box[2] - box[0]) / 72 * 25.4, (box[3] - box[1]) / 72 * 25.4)
        )
    return sizes


def _assert_fixture_pdf(path, expect_rasters=0):
    with pikepdf.open(path) as pdf:
        assert len(pdf.pages) == 2
        sizes = _page_sizes_mm(pdf)
        assert sizes[0] == pytest.approx((180, 130), abs=0.5)
        assert sizes[1] == pytest.approx((90, 60), abs=0.5)
        assert _count_raster_images(pdf) == expect_rasters


@needs_inkscape
def test_inkscape_export_is_multipage_and_fully_vector(tmp_path):
    out = tmp_path / "out.pdf"
    assert main(["export", MASTER, str(out)]) == 0
    _assert_fixture_pdf(out)


@needs_chrome
def test_chrome_export_is_multipage_and_fully_vector(tmp_path):
    out = tmp_path / "chrome.pdf"
    assert main(["export", "--backend", "chrome", MASTER, str(out)]) == 0
    _assert_fixture_pdf(out)


@needs_inkscape
def test_no_flatten_export_rasterizes_linked_svgs(tmp_path):
    out = tmp_path / "raw.pdf"
    rc = main(["export", "--no-flatten", MASTER, str(out)])
    assert rc == 2  # the post-export verify flags the unexpected rasterization
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages) == 2
        assert _count_raster_images(pdf) > 0


@needs_inkscape
def test_export_writes_flat_svg_alongside_pdf(tmp_path):
    out = tmp_path / "out.pdf"
    assert main(["export", MASTER, str(out)]) == 0
    flat = tmp_path / "out.flat.svg"
    assert flat.exists()
    # the flattened copy is itself a valid export source (hand-fix workflow)
    assert main(["export", str(flat), str(tmp_path / "roundtrip.pdf")]) == 0
    _assert_fixture_pdf(tmp_path / "roundtrip.pdf")


@needs_inkscape
def test_inkscape_page_subset(tmp_path):
    out = tmp_path / "p2.pdf"
    assert main(["export", "--pages", "2", MASTER, str(out)]) == 0
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages) == 1
        assert _page_sizes_mm(pdf)[0] == pytest.approx((90, 60), abs=0.5)


@needs_chrome
def test_chrome_page_subset(tmp_path):
    out = tmp_path / "p2c.pdf"
    assert main(["export", "--backend", "chrome", "--pages", "2", MASTER, str(out)]) == 0
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages) == 1
        assert _page_sizes_mm(pdf)[0] == pytest.approx((90, 60), abs=0.5)


def test_bad_page_spec_errors(tmp_path):
    rc = main(["export", "--pages", "7", MASTER, str(tmp_path / "x.pdf")])
    assert rc == 1


@needs_inkscape
def test_page_labels_and_title(tmp_path):
    out = tmp_path / "labeled.pdf"
    assert main(["export", MASTER, str(out)]) == 0
    with pikepdf.open(out) as pdf:
        assert str(pdf.docinfo["/Title"]) == "master"
        nums = pdf.Root.PageLabels.Nums
        assert str(nums[1].P) == "Figure 1"
        assert str(nums[3].P) == "Figure 2"


@needs_inkscape
def test_flatten_subcommand_output_renders(tmp_path):
    flat = tmp_path / "flat.svg"
    assert main(["flatten", MASTER, "-o", str(flat)]) == 0
    out = tmp_path / "from_flat.pdf"
    assert main(["export", str(flat), str(out)]) == 0
    _assert_fixture_pdf(out)


def test_pages_subcommand():
    assert main(["pages", MASTER]) == 0


@needs_inkscape
@pytest.mark.skipif(shutil.which("fc-list") is None, reason="fc-list not available")
def test_export_warns_on_missing_fonts(tmp_path, capsys):
    svg = tmp_path / "m.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="2in" height="1in" '
        'viewBox="0 0 192 96">'
        '<text x="10" y="50" font-family="DefinitelyNotARealFont123">hi</text></svg>'
    )
    rc = main(["export", "--no-flat-svg", str(svg), str(tmp_path / "o.pdf")])
    assert rc == 2
    assert "DefinitelyNotARealFont123" in capsys.readouterr().err


@needs_inkscape
def test_verify_subcommand(tmp_path):
    vector = tmp_path / "v.pdf"
    assert main(["export", MASTER, str(vector)]) == 0
    assert main(["verify", str(vector)]) == 0

    raster = tmp_path / "r.pdf"
    main(["export", "--no-flatten", "--no-verify", MASTER, str(raster)])
    assert main(["verify", str(raster)]) == 2
