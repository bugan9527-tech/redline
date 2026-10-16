"""把判定结果打成 PDF——用本机浏览器无头打印，不用装 WeasyPrint / wkhtmltopdf。

做法：先用 render.render_print_html() 渲染一份"打印用"的静态 HTML（没有控制栏和脚本、
面板全部展开、强度烤进 CSS 变量），再让 Edge/Chrome 无头打印成 PDF。这样 PDF 的淡化
和阅读页完全一致——打印引擎如实渲染 opacity。

顺带把打印用的 HTML 留在旁边（`<名字>.print.html`）：想手动微调边距或页边距，用浏览器
打开它、"打印 → 另存为 PDF"即可，不必重跑。

用法:
    python pack_pdf.py <analyzed.json> [-o out.pdf] [--strength 0.55] [--page-size A5]
                       [--chapters-per-file N] [--from N] [--to M] [--marks]
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render as RD  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

# 顺序有讲究：Edge 在这类 Windows 上最稳，Chrome 次之
BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
PAGE_SIZES = ("A5", "A4", "B5", "letter")


def find_browser() -> str | None:
    for path in BROWSER_CANDIDATES:
        if Path(path).is_file():
            return path
    for name in ("msedge", "chrome", "chromium", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def html_to_pdf(html_path: Path, pdf_path: Path, browser: str | None = None,
                timeout: int = 900) -> None:
    """无头浏览器打印。--user-data-dir 指到临时目录，避免碰到用户自己的浏览器配置。"""
    browser = browser or find_browser()
    if not browser:
        raise RuntimeError(
            "找不到 Edge / Chrome，无法生成 PDF。可以：装 Chrome/Edge，"
            "或用 --format epub 出一份 EPUB（带同样的分层样式），"
            "或打开 <名字>.print.html 自己「打印 → 另存为 PDF」。"
        )
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wnf-print-") as profile:
        cmd = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-pdf-header-footer",
            f"--user-data-dir={profile}",
            "--virtual-time-budget=60000",
            f"--print-to-pdf={pdf_path}",
            html_path.resolve().as_uri(),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout)
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        tail = ((res.stderr or "") + (res.stdout or "")).strip().splitlines()[-3:]
        raise RuntimeError("浏览器没有产出 PDF：" + " / ".join(tail))


def pdf_pages(path: Path) -> int | None:
    """能算就算，算不了返回 None（不因为缺 pypdf 就让整个流程失败）。"""
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(path)).pages)
    except Exception:
        return None


def pack_pdf(doc: dict, out: Path, strength: float = 0.55, page_size: str = "A5",
             start: int = 0, count: int | None = None, per_file: int = 0,
             marks: bool = False, browser: str | None = None, keep_html: bool = True,
             keeps=()) -> dict:
    """analyzed.json -> PDF（单文件，或每 N 章一个文件）。返回统计。"""
    page_size = page_size if page_size in PAGE_SIZES else "A5"
    chapters = doc["chapters"]
    end = len(chapters) if count is None else min(len(chapters), start + count)
    if start >= end:
        raise ValueError(f"没有可输出的章节：from={start} to={end}")

    spans = [(i, min(i + per_file, end)) for i in range(start, end, per_file)] if per_file \
        else [(start, end)]
    written = []
    for i, (a, b) in enumerate(spans, 1):
        name = out.name if len(spans) == 1 else f"{out.stem}-{i:03d}.pdf"
        target = out.parent / name
        html_path = out.parent / (target.stem + ".print.html")
        RD.render_print_html(doc, html_path, strength=strength, page_size=page_size,
                             start=a, count=b - a, keeps=keeps)
        html_to_pdf(html_path, target, browser=browser)
        pages = pdf_pages(target)
        written.append({"path": str(target), "chapters": b - a, "pages": pages,
                        "bytes": target.stat().st_size, "html": str(html_path)})
        if not keep_html:
            html_path.unlink(missing_ok=True)

    return {"files": written, "strength": strength, "page_size": page_size,
            "chapters": end - start, "browser": browser or find_browser() or ""}


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: pack_pdf.py <analyzed.json> [-o out.pdf] [--strength 0.55] "
              "[--page-size A5|A4|B5|letter] [--chapters-per-file N] [--from N] [--to M]")
        raise SystemExit(2)

    src = Path(sys.argv[1])
    out = Path(sys.argv[sys.argv.index("-o") + 1]) if "-o" in sys.argv else src.with_suffix(".pdf")
    strength = float(sys.argv[sys.argv.index("--strength") + 1]) if "--strength" in sys.argv else 0.55
    page_size = sys.argv[sys.argv.index("--page-size") + 1] if "--page-size" in sys.argv else "A5"
    per_file = int(sys.argv[sys.argv.index("--chapters-per-file") + 1]) \
        if "--chapters-per-file" in sys.argv else 0
    start = int(sys.argv[sys.argv.index("--from") + 1]) if "--from" in sys.argv else 0
    end = int(sys.argv[sys.argv.index("--to") + 1]) if "--to" in sys.argv else None

    doc = json.loads(src.read_text(encoding="utf-8"))
    count = None if end is None else max(0, end - start)
    info = pack_pdf(doc, out, strength=strength, page_size=page_size,
                    start=start, count=count, per_file=per_file, marks="--marks" in sys.argv)
    for f in info["files"]:
        pages = f"{f['pages']} 页" if f["pages"] else "页数未知"
        print(f"wrote {f['path']}  ({f['bytes'] / 1024:,.0f} KB / {f['chapters']} 章 / {pages})")


if __name__ == "__main__":
    main()
