from pathlib import Path

import pytest
from lxml import etree

from svgpages.flatten import SVGPAGES_NS, FlattenError, flatten_file, write_flattened
from svgpages.pages import SVG_NS, XLINK_NS

FIXTURES = Path(__file__).parent / "fixtures"

SVG = f"{{{SVG_NS}}}"
SRC_ATTR = f"{{{SVGPAGES_NS}}}src"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _child_svg(body: str = "", size: str = 'width="60mm" height="40mm" viewBox="0 0 60 40"') -> str:
    return (f'<svg xmlns="{SVG_NS}" xmlns:xlink="{XLINK_NS}" {size}>{body}</svg>')


@pytest.fixture(scope="module")
def flat():
    return flatten_file(FIXTURES / "master.svg")


class TestMasterFixture:
    def test_no_svg_images_remain_and_no_warnings(self, flat):
        tree, warnings = flat
        assert warnings == []
        hrefs = [
            img.get("href") or img.get(f"{{{XLINK_NS}}}href")
            for img in tree.getroot().iter(f"{SVG}image")
        ]
        assert not any(h.lower().endswith(".svg") for h in hrefs if h)

    def test_all_ids_unique(self, flat):
        tree, _ = flat
        ids = [
            el.get("id")
            for el in tree.getroot().iter()
            if isinstance(el.tag, str) and el.get("id")
        ]
        assert len(ids) == len(set(ids))

    def test_master_ids_untouched(self, flat):
        tree, _ = flat
        root = tree.getroot()
        grads = root.findall(f"{SVG}defs/{SVG}linearGradient")
        assert any(g.get("id") == "grad" for g in grads)
        rect = next(r for r in root.findall(f"{SVG}rect") if r.get("x") == "110")
        assert rect.get("fill") == "url(#grad)"

    def test_children_inlined_with_provenance(self, flat):
        tree, _ = flat
        nested = [el for el in tree.getroot().iter(f"{SVG}svg") if el.get(SRC_ATTR)]
        assert sorted(el.get(SRC_ATTR) for el in nested) == [
            "fig1/fig1.svg",
            "fig2/fig2.svg",
        ]
        for el in nested:
            assert el.get("width") == "60"
            assert el.get("height") == "40"
            assert el.get("viewBox") == "0 0 60 40"

    def test_child_refs_rewritten_consistently(self, flat):
        tree, _ = flat
        for nested in tree.getroot().iter(f"{SVG}svg"):
            src = nested.get(SRC_ATTR)
            if not src:
                continue
            grad = nested.find(f"{SVG}defs/{SVG}linearGradient")
            rect = nested.find(f"{SVG}g/{SVG}rect")
            use = nested.find(f"{SVG}g/{SVG}use")
            dot = nested.find(f"{SVG}g/{SVG}circle")
            gid = grad.get("id")
            assert gid != "grad" and gid.endswith("__grad")
            assert rect.get("fill") == f"url(#{gid})"
            assert use.get(f"{{{XLINK_NS}}}href") == f"#{dot.get('id')}"
            assert dot.get("id").endswith("__dot")

    def test_image_transform_and_id_move_to_wrapper_g(self, flat):
        tree, _ = flat
        g = next(
            el
            for el in tree.getroot().iter(f"{SVG}g")
            if el.get("id") == "placed-fig2"
        )
        assert g.get("transform") == "rotate(-3 40 80)"
        nested = g.find(f"{SVG}svg")
        assert nested is not None
        assert nested.get(SRC_ATTR) == "fig2/fig2.svg"


def test_missing_child_raises(tmp_path):
    master = _write(
        tmp_path / "m.svg",
        _child_svg('<image x="0" y="0" width="10" height="10" href="nope.svg"/>'),
    )
    with pytest.raises(FlattenError, match="not found"):
        flatten_file(master)


def test_circular_link_raises(tmp_path):
    _write(tmp_path / "a.svg",
           _child_svg('<image x="0" y="0" width="10" height="10" href="b.svg"/>'))
    _write(tmp_path / "b.svg",
           _child_svg('<image x="0" y="0" width="10" height="10" href="a.svg"/>'))
    with pytest.raises(FlattenError, match="circular"):
        flatten_file(tmp_path / "a.svg")


def test_recursive_flatten(tmp_path):
    _write(tmp_path / "a.svg",
           _child_svg('<image x="0" y="0" width="10" height="10" href="sub/b.svg"/>'))
    _write(tmp_path / "sub" / "b.svg",
           _child_svg('<image x="0" y="0" width="5" height="5" href="c.svg"/>'))
    _write(tmp_path / "sub" / "c.svg",
           _child_svg('<rect id="deep" width="1" height="1"/>'))
    tree, warnings = flatten_file(tmp_path / "a.svg")
    assert warnings == []
    rects = [el for el in tree.getroot().iter(f"{SVG}rect")]
    assert len(rects) == 1
    # doubly prefixed: once when c was inlined into b, once when b into a
    assert rects[0].get("id").endswith("__deep")
    assert rects[0].get("id").count("__") == 2


def test_raster_and_data_hrefs_untouched(tmp_path):
    master = _write(
        tmp_path / "m.svg",
        _child_svg(
            '<image x="0" y="0" width="10" height="10" href="photo.png"/>'
            '<image x="0" y="0" width="10" height="10" href="data:image/png;base64,AAAA"/>'
        ),
    )
    tree, warnings = flatten_file(master)
    assert warnings == []
    imgs = list(tree.getroot().iter(f"{SVG}image"))
    assert [i.get("href") for i in imgs] == [
        "photo.png",
        "data:image/png;base64,AAAA",
    ]


def test_preserve_aspect_ratio_copied(tmp_path):
    _write(tmp_path / "c.svg", _child_svg('<rect width="1" height="1"/>'))
    master = _write(
        tmp_path / "m.svg",
        _child_svg('<image x="0" y="0" width="10" height="10" '
                   'preserveAspectRatio="xMinYMin slice" href="c.svg"/>'),
    )
    tree, _ = flatten_file(master)
    nested = next(el for el in tree.getroot().iter(f"{SVG}svg") if el.get(SRC_ATTR))
    assert nested.get("preserveAspectRatio") == "xMinYMin slice"


def test_viewbox_synthesized_from_physical_size(tmp_path):
    _write(tmp_path / "c.svg",
           _child_svg('<rect width="1" height="1"/>', size='width="60mm" height="40mm"'))
    master = _write(
        tmp_path / "m.svg",
        _child_svg('<image x="0" y="0" width="60" height="40" href="c.svg"/>'),
    )
    tree, warnings = flatten_file(master)
    assert warnings == []
    nested = next(el for el in tree.getroot().iter(f"{SVG}svg") if el.get(SRC_ATTR))
    vb = [float(v) for v in nested.get("viewBox").split()]
    assert vb[:2] == [0, 0]
    assert vb[2] == pytest.approx(60 / 25.4 * 96, abs=0.01)
    assert vb[3] == pytest.approx(40 / 25.4 * 96, abs=0.01)


def test_stylesheet_ids_and_classes_scoped(tmp_path):
    _write(
        tmp_path / "c.svg",
        _child_svg(
            "<style>.line { stroke: url(#g); } #box { fill: red; }</style>"
            '<defs><linearGradient id="g"/></defs>'
            '<rect id="box" class="line" width="1" height="1"/>'
        ),
    )
    master = _write(
        tmp_path / "m.svg",
        _child_svg('<image x="0" y="0" width="10" height="10" href="c.svg"/>'),
    )
    tree, _ = flatten_file(master)
    nested = next(el for el in tree.getroot().iter(f"{SVG}svg") if el.get(SRC_ATTR))
    rect = nested.find(f"{SVG}rect")
    style = nested.find(f"{SVG}style").text
    assert rect.get("id").endswith("__box")
    assert rect.get("class").endswith("__line")
    assert f"#{rect.get('id')}" in style
    assert f".{rect.get('class')}" in style
    grad_id = nested.find(f"{SVG}defs/{SVG}linearGradient").get("id")
    assert f"url(#{grad_id})" in style


def test_child_editor_cruft_stripped(tmp_path):
    sodipodi = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
    _write(
        tmp_path / "c.svg",
        f'<svg xmlns="{SVG_NS}" xmlns:sodipodi="{sodipodi}" '
        'width="10" height="10" viewBox="0 0 10 10">'
        "<sodipodi:namedview/><metadata>x</metadata>"
        '<rect width="1" height="1"/></svg>',
    )
    master = _write(
        tmp_path / "m.svg",
        _child_svg('<image x="0" y="0" width="10" height="10" href="c.svg"/>'),
    )
    tree, _ = flatten_file(master)
    nested = next(el for el in tree.getroot().iter(f"{SVG}svg") if el.get(SRC_ATTR))
    tags = [el.tag for el in nested]
    assert f"{SVG}rect" in tags
    assert f"{{{sodipodi}}}namedview" not in tags
    assert f"{SVG}metadata" not in tags


def test_write_flattened_rebases_raster_hrefs(tmp_path):
    master = _write(
        tmp_path / "figs" / "m.svg",
        _child_svg('<image x="0" y="0" width="10" height="10" href="photo.png"/>'),
    )
    tree, _ = flatten_file(master)
    out = tmp_path / "elsewhere" / "flat.svg"
    out.parent.mkdir()
    write_flattened(tree, out, master.parent)
    reparsed = etree.parse(str(out))
    img = next(reparsed.getroot().iter(f"{SVG}image"))
    assert img.get("href") == "../figs/photo.png"
