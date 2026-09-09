from pathlib import Path

import pytest
from lxml import etree

from svgpages.pages import (
    Page,
    get_pages,
    length_to_inches,
    parse_page_spec,
    parse_pages,
)

FIXTURES = Path(__file__).parent / "fixtures"

INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
SODIPODI_NS = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"


def _tree(s: str) -> etree._ElementTree:
    return etree.ElementTree(etree.fromstring(s.encode()))


@pytest.mark.parametrize(
    "value,inches",
    [
        ("25.4mm", 1.0),
        ("2.54cm", 1.0),
        ("1in", 1.0),
        ("72pt", 1.0),
        ("6pc", 1.0),
        ("96px", 1.0),
        ("96", 1.0),
        (" 48 ", 0.5),
    ],
)
def test_length_to_inches(value, inches):
    assert length_to_inches(value) == pytest.approx(inches)


def test_master_fixture_pages():
    pages, _ = get_pages(FIXTURES / "master.svg")
    assert len(pages) == 2
    p1, p2 = pages
    assert p1.id == "page01"
    assert p1.label == "Figure 1"
    assert (p1.x, p1.y, p1.w, p1.h) == (0, 0, 180, 130)
    assert p1.w_mm == pytest.approx(180)
    assert p1.h_mm == pytest.approx(130)
    assert p2.label == "Figure 2"
    assert p2.x == 190
    assert p2.w_mm == pytest.approx(90)
    assert p2.h_mm == pytest.approx(60)


def test_viewbox_scale_differs_from_user_units():
    # 0.25 mm per user unit: width=180mm but viewBox spans 720 units
    tree = _tree(f"""
    <svg xmlns="http://www.w3.org/2000/svg"
         xmlns:inkscape="{INKSCAPE_NS}" xmlns:sodipodi="{SODIPODI_NS}"
         width="180mm" height="130mm" viewBox="0 0 720 520">
      <sodipodi:namedview>
        <inkscape:page x="0" y="0" width="720" height="520"/>
      </sodipodi:namedview>
    </svg>""")
    (page,) = parse_pages(tree)
    assert page.w_mm == pytest.approx(180)
    assert page.h_mm == pytest.approx(130)


def test_no_pages_fallback_with_viewbox():
    tree = _tree("""
    <svg xmlns="http://www.w3.org/2000/svg"
         width="4in" height="2in" viewBox="0 0 384 192"/>""")
    (page,) = parse_pages(tree)
    assert page.label is None
    assert (page.w, page.h) == (384, 192)
    assert page.w_in == pytest.approx(4)
    assert page.h_in == pytest.approx(2)


def test_no_pages_no_viewbox_bare_px():
    tree = _tree("""
    <svg xmlns="http://www.w3.org/2000/svg" width="96" height="192"/>""")
    (page,) = parse_pages(tree)
    assert page.w_in == pytest.approx(1)
    assert page.h_in == pytest.approx(2)


def test_page_id_formatting():
    p = Page(3, None, 0, 0, 10, 10, 1, 1)
    assert p.id == "page03"


@pytest.mark.parametrize(
    "spec,n,expected",
    [
        ("1", 5, [1]),
        ("1,3-5", 5, [1, 3, 4, 5]),
        ("3-5,1,4", 5, [1, 3, 4, 5]),
        (" 2 , 4 ", 4, [2, 4]),
    ],
)
def test_parse_page_spec(spec, n, expected):
    assert parse_page_spec(spec, n) == expected


@pytest.mark.parametrize("spec", ["", "0", "6", "2-1", "1-", "a", "1,,2", "1-3", "-2"])
def test_parse_page_spec_invalid(spec):
    with pytest.raises(ValueError):
        parse_page_spec(spec, 2)
