"""打包自检：EPUB 的结构与分层样式，以及 PDF 里的淡化是否**真的**落到了纸上。

机器上通常没有 epubcheck，所以能自己查的都自己查：zip 结构、XML 合法性、清单与脊柱一致、
XHTML 里的 class 都在样式表里、行内样式与样式表声明一致、正文逐字保真、分层深浅单调。
PDF 则更进一步：用 poppler 把页面渲成图，量"注水"行与正文行的墨色——淡化不能只写在
样式里，得在页面上看得见。

用法:
    python tests/check_pack.py <analyzed.json> [--skip-pdf]

PDF 那部分需要 Edge/Chrome；墨色测量需要 poppler 与 Pillow，缺了会跳过并说明。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from lxml import etree

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
import pack_epub as EP  # noqa: E402
import pack_pdf as PDF  # noqa: E402
import detect_rules as DR  # noqa: E402
import parse as P  # noqa: E402
from tiers import tier_of  # noqa: E402

TMP = Path(os.environ.get("TEMP", "/tmp")) / "wnf-pack-check"
FIXTURE = """第一章 分层测试

【等级：3】
【力量：7】
普通的一句话，用来当作正文基准行，长度中等，方便比对墨色。
但转折就在这里，突然冒出关键情节。
"""


def lum(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.299 * r + 0.587 * g + 0.114 * b


def epub_checks(doc: dict, tmp: Path) -> list[str]:
    problems: list[str] = []
    out = tmp / "book.epub"
    info = EP.pack_epub(doc, out, strength=0.55, modified="2026-01-01T00:00:00Z")
    zf = zipfile.ZipFile(out)
    names = zf.namelist()

    first = zf.infolist()[0]
    if first.filename != "mimetype" or first.compress_type != zipfile.ZIP_STORED:
        problems.append("mimetype 不是第一个条目，或被压缩了")
    if zf.read("mimetype") != b"application/epub+zip":
        problems.append("mimetype 内容不对")

    root = etree.fromstring(zf.read("META-INF/container.xml"))
    opf_path = root.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile").get("full-path")
    if opf_path not in names:
        problems.append(f"container 指向的 {opf_path} 不在包里")

    for name in names:
        if name.endswith((".xhtml", ".opf", ".ncx", ".xml", ".css")):
            try:
                if name.endswith(".css"):
                    zf.read(name).decode("utf-8")
                else:
                    etree.fromstring(zf.read(name))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{name} 解析失败：{str(exc)[:70]}")

    opf = etree.fromstring(zf.read(opf_path))
    NS = {"opf": "http://www.idpf.org/2007/opf"}
    manifest = {it.get("id"): it.get("href") for it in opf.findall(".//opf:item", NS)}
    spine = [ref.get("idref") for ref in opf.findall(".//opf:itemref", NS)]
    for href in manifest.values():
        if f"OEBPS/{href}" not in names:
            problems.append(f"manifest 里的 {href} 不在包里")
    for idref in spine:
        if idref not in manifest:
            problems.append(f"spine 引用了不存在的 {idref}")
    if len(spine) - 1 != len(doc["chapters"]):
        problems.append(f"spine 章数 {len(spine) - 1} 与数据 {len(doc['chapters'])} 不一致")

    nav = zf.read("OEBPS/nav.xhtml").decode("utf-8")
    hrefs = [h for h in manifest.values() if h.startswith("ch")]
    missing = [h for h in hrefs if f'href="{h}"' not in nav]
    if missing:
        problems.append(f"nav.xhtml 漏了 {len(missing)} 章")

    css = zf.read("OEBPS/style.css").decode("utf-8")
    defined = set(__import__("re").findall(r"p\.(t-\w+)\s*\{", css))
    used = set(__import__("re").findall(r'<p class="(t-\w+)"', zf.read("OEBPS/ch0001.xhtml").decode("utf-8")))
    if used - defined:
        problems.append(f"XHTML 用了样式表里没有的 class：{sorted(used - defined)}")

    # 行内样式与 class 声明必须一致：两套机制漂移的话，阅读器清洗掉 <style> 就会换一副面孔
    import re
    inline = dict(re.findall(r'<p class="(t-\w+)" style="([^"]+)"', zf.read("OEBPS/ch0001.xhtml").decode("utf-8")))
    for klass, decls in inline.items():
        block = re.search(r"p\." + klass + r"\s*\{([^}]*)\}", css)
        if not block or decls.strip() not in block.group(1).strip():
            problems.append(f"{klass} 的行内样式与样式表不一致：{decls[:60]}")

    # 正文保真：抽 5 段逐字比对
    src_texts = [p["text"] for ch in doc["chapters"][:3] for p in ch["paragraphs"]][:5]
    html_all = "".join(zf.read(n).decode("utf-8") for n in names if n.endswith(".xhtml"))
    for text in src_texts:
        needle = text.replace("&", "&amp;").replace("<", "&lt;")
        if needle not in html_all:
            problems.append(f"正文没逐字保真：{text[:24]}…")

    # 分层深浅必须单调：plot 最深，weak < fade < zero 依次更浅
    base = lum(EP.BASE_INK)
    colors = {t: lum(EP.tier_decls(t, 0.55, "color")["color"]) for t in ("weak", "fade", "zero")}
    if not (lum(EP.tier_decls("plot", 0.55, "color")["color"]) <= base
            < colors["weak"] < colors["fade"] < colors["zero"]):
        problems.append(f"分层深浅不单调：{colors}")
    plain = {t for t in ("weak", "fade", "zero") if not EP.tier_decls(t, 0.0, "color")}
    if plain != {"weak", "fade", "zero"}:
        problems.append(f"强度 0 时仍有淡化样式：{plain}")

    print(f"    [epub] ok  {info['bytes'] / 1024:,.0f} KB / {info['chapters']} 章 / "
          f"{len(names)} 个条目 / 分层深浅 {base:.0f} < {colors['weak']:.0f} < "
          f"{colors['fade']:.0f} < {colors['zero']:.0f}")
    return problems


def ink_bands(png: Path) -> list[tuple[int, int, float]]:
    """把渲染图切成"有字的行带"，返回每带 (起始行, 结束行, 最黑灰度)。"""
    import numpy as np
    from PIL import Image

    img = np.asarray(Image.open(png).convert("L"), dtype=float)
    dark = (img < 200).sum(axis=1)
    bands, start = [], None
    for y, n in enumerate(dark):
        if n > 0 and start is None:
            start = y
        elif n == 0 and start is not None:
            if y - start >= 6:
                bands.append((start, y, img[start:y].min()))
            start = None
    if start is not None:
        bands.append((start, len(dark), img[start:].min()))
    return bands


def pdf_fade_check(tmp: Path) -> list[str]:
    poppler = shutil.which("pdftoppm")
    if not poppler:
        print("    [pdf ] 跳过墨色测量：找不到 poppler（pdftoppm）")
        return []
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:
        print("    [pdf ] 跳过墨色测量：缺 numpy / Pillow")
        return []

    # 固定样本：一段普通、一段关键情节、两行系统面板（面板 = 注水层）
    src = tmp / "fixture.txt"
    src.write_text(FIXTURE, encoding="utf-8")
    doc = DR.analyze(P.parse_file(src))
    tiers = [tier_of(p["kind"], p["score"]) for p in doc["chapters"][0]["paragraphs"]]
    if tiers != ["fade", "fade", "mid", "plot"]:
        return [f"样本分层与预期不符（{tiers}），测试需要跟着更新"]

    info = PDF.pack_pdf(doc, tmp / "fixture.pdf", strength=0.55, page_size="A5")
    pdf_path = Path(info["files"][0]["path"])
    subprocess.run([poppler, "-png", "-r", "100", "-f", "1", "-l", "1",
                    str(pdf_path), str(tmp / "page")], check=True, capture_output=True)
    png = next(tmp.glob("page*.png"))

    bands = [b for b in ink_bands(png) if b[1] - b[0] < 60]  # 标题带更高，去掉
    if len(bands) < 4:
        return [f"页面上找到的文字行带只有 {len(bands)} 个，无法比对"]
    darkest = [b[2] for b in bands[-4:]]  # 顺序即：面板两行、普通、情节
    panel, mid, plot = max(darkest[0], darkest[1]), darkest[2], darkest[3]
    if not (plot < mid < panel):
        return [f"淡化没落到纸上：情节 {plot:.0f} / 普通 {mid:.0f} / 面板 {panel:.0f}"]
    print(f"    [pdf ] ok  最黑灰度 情节 {plot:.0f} < 普通 {mid:.0f} < 注水面板 {panel:.0f}"
          f"（页数 {info['files'][0]['pages']}）")
    return []


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: check_pack.py <analyzed.json> [--skip-pdf]")
        raise SystemExit(2)
    doc = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    TMP.mkdir(parents=True, exist_ok=True)

    problems = epub_checks(doc, TMP)
    if "--skip-pdf" not in sys.argv:
        if PDF.find_browser():
            problems += pdf_fade_check(TMP)
        else:
            print("    [pdf ] 跳过：找不到 Edge / Chrome")

    for p in problems:
        print("    ! " + p)
    print("\n结论：" + ("打包检查通过" if not problems else f"{len(problems)} 项不通过"))
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
