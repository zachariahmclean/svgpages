"""PDF assembly: merge per-page PDFs, subset pages, page labels, metadata."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

import pikepdf

from . import __version__


def merge_page_pdfs(page_pdfs: list[Path], out_pdf: Path | str,
                    labels: list[str | None], title: str | None,
                    warnings: list[str]) -> None:
    """Merge single-page PDFs (one per document page) into out_pdf.

    Keeps only the first page of each input: renderers occasionally emit a
    spurious trailing blank page (print-overflow rounding); anything beyond
    one page is reported as a warning.
    """
    merged = pikepdf.new()
    with ExitStack() as stack:
        for path in page_pdfs:
            src = stack.enter_context(pikepdf.open(path))
            if len(src.pages) > 1:
                warnings.append(
                    f"{Path(path).name}: renderer produced {len(src.pages)} "
                    "pages; keeping only the first"
                )
            merged.pages.append(src.pages[0])
        apply_labels_and_title(merged, labels, title)
        merged.save(out_pdf)


def finalize_pdf(src_pdf: Path | str, out_pdf: Path | str,
                 keep: list[int] | None, labels: list[str | None],
                 title: str | None) -> None:
    """Copy src_pdf to out_pdf keeping only 1-based pages in ``keep``
    (None = all), applying page labels (parallel to the kept pages) and
    document metadata."""
    with pikepdf.open(src_pdf) as pdf:
        if keep is not None:
            drop = [i for i in range(1, len(pdf.pages) + 1) if i not in keep]
            for i in reversed(drop):
                del pdf.pages[i - 1]
        apply_labels_and_title(pdf, labels, title)
        pdf.save(out_pdf)


def apply_labels_and_title(pdf: pikepdf.Pdf, labels: list[str | None],
                           title: str | None) -> None:
    if any(labels):
        nums = pikepdf.Array()
        for i, label in enumerate(labels):
            nums.append(i)
            nums.append(pikepdf.Dictionary(P=pikepdf.String(label or str(i + 1))))
        pdf.Root.PageLabels = pikepdf.Dictionary(Nums=nums)
    if title:
        pdf.docinfo["/Title"] = title
    pdf.docinfo["/Producer"] = f"svgpages {__version__}"
