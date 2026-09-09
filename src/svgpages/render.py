"""Locate rendering backends and drive them.

Two backends, both operating on a flattened document:

- **inkscape** (default): Inkscape >= 1.2 exports a Pages document to a
  native multi-page PDF in one CLI call, fully vector once flattened.
- **chrome**: per page, the cropped SVG is inlined into a minimal HTML
  wrapper with an explicit ``@page`` rule (printing a raw SVG defaults to
  Letter/A4) and printed via headless Chrome, then merged. The wrapper
  needs ``svg { display: block }`` and hidden overflow, or the inline-SVG
  whitespace overflows into a spurious trailing blank page.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from lxml import etree

from .pages import Page


class RenderError(Exception):
    """A rendering backend failed or produced no output."""


_INKSCAPE_CANDIDATES = [
    "/usr/bin/inkscape",
    "/usr/local/bin/inkscape",
    "/Applications/Inkscape.app/Contents/MacOS/inkscape",
    r"C:\Program Files\Inkscape\bin\inkscape.exe",
    # Windows Inkscape reachable from WSL
    "/mnt/c/Program Files/Inkscape/bin/inkscape.exe",
]


def find_inkscape(explicit: str | None = None) -> str | None:
    candidates = [
        explicit,
        os.environ.get("SVGPAGES_INKSCAPE"),
        shutil.which("inkscape"),
        *_INKSCAPE_CANDIDATES,
    ]
    for c in candidates:
        if c and Path(c).exists():
            return str(c)
    return None


def _is_windows_exe_under_wsl(tool: str) -> bool:
    return tool.startswith("/mnt/") and tool.lower().endswith(".exe")


_WSL_WINDOWS_TOOLS = {
    "inkscape": "/mnt/c/Program Files/Inkscape/bin/inkscape.exe",
    "chrome": "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
}


def find_windows_tool(backend: str) -> str | None:
    """Path of the Windows binary for a backend, when reachable from WSL.

    Windows binaries resolve fonts from the Windows font store — the right
    choice for documents authored on Windows (e.g. using Arial)."""
    path = _WSL_WINDOWS_TOOLS.get(backend)
    return path if path and Path(path).exists() else None


def _tool_path(tool: str, path: Path) -> str:
    """Translate a path for the tool's view of the filesystem: Windows
    binaries invoked from WSL can't read /home-style paths."""
    if not _is_windows_exe_under_wsl(tool):
        return str(path)
    # wslpath -w needs an existing path; the file itself may not exist yet
    # (output PDFs), so translate the parent directory and re-attach the name.
    result = subprocess.run(
        ["wslpath", "-w", str(path.parent)], capture_output=True, text=True, check=True
    )
    return result.stdout.strip() + "\\" + path.name


def export_pdf_inkscape(inkscape: str, svg_path: Path | str, pdf_path: Path | str,
                        timeout: int = 300) -> str:
    """Export svg_path to a (multi-page) PDF at pdf_path. Returns
    Inkscape's stderr (warnings) for optional surfacing."""
    svg_path = Path(svg_path).resolve()
    pdf_path = Path(pdf_path).resolve()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        inkscape,
        _tool_path(inkscape, svg_path),
        "--export-type=pdf",
        f"--export-filename={_tool_path(inkscape, pdf_path)}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0 or not pdf_path.exists():
        raise RenderError(
            f"Inkscape export failed (exit {result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result.stderr


_CHROME_CANDIDATES = [
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    # Windows Chrome reachable from WSL
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
]


def find_chrome(explicit: str | None = None) -> str | None:
    candidates = [
        explicit,
        os.environ.get("SVGPAGES_CHROME"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("chrome"),
        *_CHROME_CANDIDATES,
    ]
    for c in candidates:
        if c and Path(c).exists():
            return str(c)
    return None


_HTML_WRAPPER = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: {w_in:.6f}in {h_in:.6f}in; margin: 0; }}
html, body {{ margin: 0; padding: 0; overflow: hidden; }}
svg {{ display: block; }}
</style></head>
<body>{svg}</body></html>
"""


def write_page_html(cropped_root: etree._Element, page: Page, out_html: Path) -> None:
    """Write the HTML @page wrapper with the cropped SVG inlined directly
    in the body (an <img> reference could re-rasterize)."""
    svg_markup = etree.tostring(cropped_root, encoding="unicode")
    out_html.write_text(
        _HTML_WRAPPER.format(w_in=page.w_in, h_in=page.h_in, svg=svg_markup),
        encoding="utf-8",
    )


def print_page_pdf_chrome(chrome: str, html_path: Path | str,
                          pdf_path: Path | str, timeout: int = 120) -> None:
    """Print one wrapper HTML to PDF with headless Chrome. Retries once
    with --no-sandbox, which some container/WSL setups require."""
    html_path = Path(html_path).resolve()
    pdf_path = Path(pdf_path).resolve()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    if _is_windows_exe_under_wsl(chrome):
        input_arg = _tool_path(chrome, html_path)
    else:
        input_arg = html_path.as_uri()
    cmd = [
        chrome, "--headless=new", "--disable-gpu",
        "--print-to-pdf-no-header",
        f"--print-to-pdf={_tool_path(chrome, pdf_path)}",
        input_arg,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0 or not pdf_path.exists():
        cmd_retry = cmd[:1] + ["--no-sandbox"] + cmd[1:]
        result = subprocess.run(cmd_retry, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0 or not pdf_path.exists():
        raise RenderError(
            f"Chrome failed to print {html_path.name} "
            f"(exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
        )
