"""把判定结果打包成 EPUB——真正能在阅读器里"淡化"的载体。

为什么不是 txt：txt 没有样式，"淡化"只能靠丢段或加文字标记；EPUB 是 XHTML，
颜色和字重都能真生效（支持 opacity 的阅读器还能更接近页面所见）。

淡化用哪个杠杆是可选的（`--lever`），因为各家阅读器保留的样式不一样：

  color    灰色文字。兼容性最好，深色主题下也读得出来（默认）
  opacity  透明度。视觉上最接近阅读页，但部分阅读器（尤其 Kindle 系）会忽略，
           忽略后退化成原文，不会出错

拿不准就用仓库里的 experiments/make_css_probe.py 生成样式探针 EPUB，导进你的阅读器
看一眼哪些手段活着，再定 --lever。

打包本身零依赖：zipfile + 手写 XHTML，不装 ebooklib。

用法:
    python pack_epub.py <analyzed.json> [-o out.epub] [--strength 0.55]
                        [--keep sys_panel,scenery] [--panel full|collapse] [--lever color]
"""

from __future__ import annotations

import json
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tiers import tier_of  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

MIMETYPE = "application/epub+zip"
CSS_HREF = "style.css"

# 淡化系数与阅读页共用一套口径（render.py 的 DIM）：不透明度 = 1 - 强度 × 系数。
# 这里唯一的新问题是"txt/EPUB 没有透明度"——EPUB 有颜色，所以把不透明度换算成
# 等价的灰色：颜色 = 前景色按 alpha 混合到纸张白。这样 EPUB 看起来和阅读页一致。
DIM = {"zero": 0.82, "fade": 0.58, "weak": 0.28, "mid": 0.10, "plot": 0.0}
BASE_INK = "#333333"
PLOT_INK = "#111111"
LEVERS = ("color", "opacity")

# 读者"保留原文"的分组：这些段落不打淡化样式（和阅读页勾选框一个含义）
KEEP_FLAGS = (("scenery", "scenery"), ("short_burst", "short_burst"),
              ("recap", "recap"), ("dialogue", "dialogue_scene"))


def is_kept(p: dict, keeps) -> bool:
    if p["kind"] == "sys_panel" and "sys_panel" in keeps:
        return True
    if tier_of(p["kind"], p["score"]) == "zero" and "junk" in keeps:
        return True
    return any(flag in p["flags"] and key in keeps for key, flag in KEEP_FLAGS)


def blend_to_paper(hex_color: str, alpha: float, paper: str = "#ffffff") -> str:
    fg = [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]
    bg = [int(paper[i:i + 2], 16) for i in (1, 3, 5)]
    mix = [round(alpha * f + (1 - alpha) * b) for f, b in zip(fg, bg)]
    return "#%02x%02x%02x" % tuple(mix)


def tier_decls(tier: str, strength: float, lever: str, emphasis: bool = True) -> dict[str, str]:
    """一个分层在这一强度下该长什么样。返回空字典 = 保持原文（普通层就是原文）。"""
    if tier == "mid":
        return {}
    if tier == "sep":
        return {"color": "#c0c0c0"}
    if tier == "plot":
        decls = {"color": PLOT_INK}
        if emphasis:
            decls["font-weight"] = "600"
        return decls
    alpha = 1 - max(0.0, min(1.0, strength)) * DIM.get(tier, 0.0)
    if lever == "opacity":
        return {} if alpha > 0.98 else {"opacity": f"{alpha:.2f}"}
    if alpha > 0.97:  # 已经和正文色几乎一样，不必写样式
        return {}
    return {"color": blend_to_paper(BASE_INK, alpha)}


def decl_text(decls: dict[str, str]) -> str:
    return "; ".join(f"{k}: {v}" for k, v in decls.items())


def stylesheet(lever: str, strength: float, emphasis: bool = True) -> str:
    """整本书共用一份 CSS。正文同时挂 class 和行内样式：
    阅读器若清洗掉 <style> 块，行内样式还能兜底。"""
    rows = [
        "/* 由 webnovel-focus 生成：分层渲染样式。字体不内嵌，交给阅读器 */",
        f"body {{ margin: 0 4%; line-height: 1.75; color: {BASE_INK}; }}",
        "h1 { font-size: 1.2em; font-weight: 600; text-align: center; margin: 1.6em 0 1em; }",
        "p { margin: 0 0 0.72em; text-indent: 0; }",
        "hr { border: 0; border-top: 1px solid #d8d8d8; width: 40%; margin: 1.6em auto; }",
        "p.note { color: #9b9b9b; font-size: 0.92em; }",
    ]
    for tier in ("plot", "weak", "fade", "zero"):
        decls = tier_decls(tier, strength, lever, emphasis)
        if decls:
            rows.append(f"p.t-{tier} {{ {decl_text(decls)}; }}")
    return "\n".join(rows) + "\n"


def clean_text(text: str) -> str:
    """XML 不允许的 C0 控制字符直接去掉，避免整章 XHTML 打不开。"""
    return "".join(c for c in text if c >= " " or c in "\t")


def effective_decls(p: dict, strength: float, lever: str, emphasis: bool = True,
                    keeps=()) -> tuple[str, dict[str, str]]:
    """这一段最终该写什么样式。被读者"保留原文"的段落不打淡化（重点加粗仍然保留）。"""
    tier = tier_of(p["kind"], p["score"])
    if keeps and is_kept(p, keeps) and tier != "plot":
        return tier, {}
    return tier, tier_decls(tier, strength, lever, emphasis)


def para_html(p: dict, lever: str, strength: float, emphasis: bool = True,
              keeps=()) -> str:
    tier, decls = effective_decls(p, strength, lever, emphasis, keeps)
    style = f" style={quoteattr(decl_text(decls))}" if decls else ""
    klass = f' class="t-{tier}"' if decls else ""
    return f"<p{klass}{style}>{escape(clean_text(p['text']))}</p>"


def chapter_xhtml(ch: dict, lever: str, panel: str, strength: float = 0.55,
                  emphasis: bool = True, keeps=()) -> str:
    title = (ch.get("title") or f"第 {ch['index']} 章").strip()
    body: list[str] = [f"<h1>{escape(clean_text(title))}</h1>"]
    buf: list[dict] = []

    def flush() -> None:
        if not buf:
            return
        if panel == "collapse" and len(buf) > 1:
            # EPUB 里读者没法展开，所以默认不折叠；选了 collapse 就明说省了多少行
            body.append(para_html(buf[0], lever, strength, emphasis, keeps))
            body.append(f'<p class="note">〔面板：略去 {len(buf) - 1} 行〕</p>')
        else:
            body.extend(para_html(p, lever, strength, emphasis, keeps) for p in buf)
        buf.clear()

    for p in ch["paragraphs"]:
        if "panel_block" in p["flags"]:
            buf.append(p)
            continue
        flush()
        if p["kind"] == "separator":
            body.append("<hr/>")
        else:
            body.append(para_html(p, lever, strength, emphasis, keeps))
    flush()

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">\n'
        f"<head><title>{escape(clean_text(title))}</title>"
        f'<link rel="stylesheet" type="text/css" href="{CSS_HREF}"/></head>\n'
        f"<body>\n" + "\n".join(body) + "\n</body>\n</html>\n"
    )


def titlepage_xhtml(title: str, meta_lines: list[str]) -> str:
    rows = "\n".join(f'<p class="note">{escape(clean_text(l))}</p>' for l in meta_lines)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">\n'
        f"<head><title>{escape(clean_text(title))}</title>"
        f'<link rel="stylesheet" type="text/css" href="{CSS_HREF}"/></head>\n'
        f"<body>\n<h1>{escape(clean_text(title))}</h1>\n{rows}\n</body>\n</html>\n"
    )


def nav_xhtml(title: str, toc: list[dict]) -> str:
    items = "\n".join(
        f'<li><a href="{t["href"]}">{escape(clean_text(t["title"]))}</a></li>' for t in toc
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        'xml:lang="zh-CN" lang="zh-CN">\n'
        f"<head><title>目录</title></head>\n"
        f'<body>\n<nav epub:type="toc" id="toc">\n<h1>{escape(clean_text(title))}</h1>\n'
        f"<ol>\n{items}\n</ol>\n</nav>\n</body>\n</html>\n"
    )


def ncx(title: str, toc: list[dict]) -> str:
    """EPUB2 时代的目录，老的阅读器（以及部分不支持 EPUB3 nav 的）只认它。"""
    points = "\n".join(
        f'<navPoint id="n{i + 1}" playOrder="{i + 1}">'
        f'<navLabel><text>{escape(clean_text(t["title"]))}</text></navLabel>'
        f'<content src="{t["href"]}"/></navPoint>'
        for i, t in enumerate(toc)
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        f"<head><meta name=\"dtb:uid\" content=\"{escape(title)}\"/></head>\n"
        f"<docTitle><text>{escape(clean_text(title))}</text></docTitle>\n"
        f"<navMap>\n{points}\n</navMap>\n</ncx>\n"
    )


def opf(title: str, book_id: str, modified: str, manifest: list[tuple[str, str, str]],
        spine: list[str], description: str) -> str:
    items = "\n".join(
        f'<item id="{mid}" href="{href}" media-type="{mt}"{props}/>'
        for mid, href, mt, props in manifest
    )
    refs = "\n".join(f'<itemref idref="{s}"/>' for s in spine)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="BookId" xml:lang="zh-CN">\n'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f"<dc:title>{escape(clean_text(title))}</dc:title>\n"
        "<dc:language>zh-CN</dc:language>\n"
        f'<dc:identifier id="BookId">{escape(book_id)}</dc:identifier>\n'
        f"<dc:description>{escape(clean_text(description))}</dc:description>\n"
        f'<meta property="dcterms:modified">{modified}</meta>\n'
        "</metadata>\n"
        f"<manifest>\n{items}\n</manifest>\n"
        f'<spine toc="ncx">\n{refs}\n</spine>\n'
        "</package>\n"
    )


CONTAINER = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def pack_epub(doc: dict, out: Path, strength: float = 0.55, keeps=(), panel: str = "full",
              lever: str = "color", title: str = "", modified: str | None = None,
              start: int = 0, count: int | None = None, emphasis: bool = True) -> dict:
    """analyzed.json -> .epub。返回一份统计，供调用方打印与自检。"""
    lever = lever if lever in LEVERS else "color"
    chapters = doc["chapters"][start:] if count is None else doc["chapters"][start:start + count]
    if not chapters:
        raise ValueError(f"没有可打包的章节：start={start} count={count}")

    book_title = title or Path(doc.get("source") or "未命名").stem
    book_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, 'webnovel-focus:' + book_title)}"
    modified = modified or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    desc = (f"由 webnovel-focus 生成 · 强度 {strength:.2f} · 淡化杠杆 {lever} · "
            f"面板 {panel} · 保留原文 {'、'.join(keeps) if keeps else '无'}")

    toc, files, manifest, spine = [], {}, [], []
    for i, ch in enumerate(chapters):
        name = f"ch{i + 1:04d}.xhtml"
        mid = f"c{i + 1}"
        files[f"OEBPS/{name}"] = chapter_xhtml(ch, lever, panel, strength, emphasis, keeps)
        toc.append({"href": name, "title": (ch.get("title") or f"第 {ch['index']} 章").strip()})
        manifest.append((mid, name, "application/xhtml+xml", ""))
        spine.append(mid)

    files["OEBPS/mimetype"] = MIMETYPE
    files["OEBPS/style.css"] = stylesheet(lever, strength, emphasis)
    files["OEBPS/titlepage.xhtml"] = titlepage_xhtml(
        book_title, [doc.get("source", ""), desc, f"{len(chapters)} 章"])
    files["OEBPS/nav.xhtml"] = nav_xhtml(book_title, toc)
    files["OEBPS/toc.ncx"] = ncx(book_title, toc)
    files["OEBPS/content.opf"] = opf(
        book_title, book_id, modified,
        [("nav", "nav.xhtml", "application/xhtml+xml", ' properties="nav"'),
         ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
         ("css", CSS_HREF, "text/css", ""),
         ("title", "titlepage.xhtml", "application/xhtml+xml", "")] + manifest,
        ["title"] + spine, desc)

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w") as zf:
        # mimetype 必须是第一个条目且不压缩，否则部分阅读器直接拒收
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, MIMETYPE)
        zf.writestr("META-INF/container.xml", CONTAINER, compress_type=zipfile.ZIP_DEFLATED)
        for name, text in files.items():
            if name == "OEBPS/mimetype":
                continue
            zf.writestr(name, text, compress_type=zipfile.ZIP_DEFLATED)

    return {"path": str(out), "chapters": len(chapters), "toc": len(toc),
            "bytes": out.stat().st_size, "lever": lever,
            "graded": sum(1 for ch in chapters for p in ch["paragraphs"]
                          if p["kind"] != "separator"
                          and effective_decls(p, strength, lever, emphasis, keeps)[1])}


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: pack_epub.py <analyzed.json> [-o out.epub] [--strength 0.55] "
              "[--keep sys_panel,scenery] [--panel full|collapse] [--lever color|opacity]")
        raise SystemExit(2)

    src = Path(sys.argv[1])
    out = Path(sys.argv[sys.argv.index("-o") + 1]) if "-o" in sys.argv else src.with_suffix(".epub")
    strength = float(sys.argv[sys.argv.index("--strength") + 1]) if "--strength" in sys.argv else 0.55
    keeps = sys.argv[sys.argv.index("--keep") + 1].split(",") if "--keep" in sys.argv else []
    panel = sys.argv[sys.argv.index("--panel") + 1] if "--panel" in sys.argv else "full"
    lever = sys.argv[sys.argv.index("--lever") + 1] if "--lever" in sys.argv else "color"

    doc = json.loads(src.read_text(encoding="utf-8"))
    info = pack_epub(doc, out, strength=strength, keeps=keeps, panel=panel, lever=lever)
    print(f"wrote {info['path']}  ({info['bytes'] / 1024:,.0f} KB / {info['chapters']} 章 / "
          f"{info['graded']:,} 段带分层样式 · 淡化杠杆 {info['lever']})")


if __name__ == "__main__":
    main()
