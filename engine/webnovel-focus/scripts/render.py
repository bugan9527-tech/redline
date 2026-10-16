"""把分析结果渲染成自包含的 HTML。

两种模式：
  - 单文件：整本塞进一个 HTML，双击即读。适合节选（几十章）。
  - 分章：目录页 + 每章一个文件。全书规模必须用这个——实测 369 万字的书
    塞成单文件是 27.7 MB / 11 万个 DOM 节点，Chrome 加载要 12 秒，手机上
    基本不可用；拆成每章约 20~90 KB，打开是瞬时的。

偏好通过 URL 查询串在页面之间传递。不用 localStorage 是因为 file:// 下
各浏览器的同源策略不一致，查询串是唯一可靠的跨文件传递方式。

用法:
    python render.py <analyzed.json> [-o 输出] [--mode auto|single|split] [--marks]

--marks 让页面默认打开「显示判定」（每段画出分层标签与判定证据）。给用户
抽查判定是否合理时用；正式阅读不打开，读者自己按需勾选。

``render_doc(doc, out, mode, marks)`` 是同一套逻辑的可调用入口，供 run.py 用。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jinja2 import Template

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tiers import tier_of  # noqa: E402

ASSETS = Path(__file__).resolve().parent.parent / "assets"

DIM = {"zero": 0.82, "fade": 0.58, "weak": 0.28, "mid": 0.10, "plot": 0.0, "sep": 0.0}
# 读者"保留原文"的分组名（与模板里的 data-keep 一一对应）
KEEP_KEYS = ("sys_panel", "scenery", "short_burst", "recap", "dialogue", "junk")
TIER_LABEL = {
    "zero": "零信息",
    "fade": "注水",
    "weak": "偏水",
    "mid": "普通",
    "plot": "情节",
    "sep": "分隔",
}

# 超过这个字数自动分章：单文件会到 3 MB 以上，加载开始卡顿
SPLIT_AT_CHARS = 400_000

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass


def view_of(p: dict) -> dict:
    tier = tier_of(p["kind"], p["score"])
    return {
        **p,
        "tier": tier,
        "dim": DIM[tier],
        "tier_label": TIER_LABEL[tier],
        "flags_attr": " ".join(p["flags"]),
        "tip": " / ".join(p["evidence"]) or "无注水特征",
    }


def flow_of(chapter: dict) -> list[dict]:
    """把段落序列编成「段落 / 面板块 / 分隔线」三种流元素。"""
    flow: list[dict] = []
    panel_buf: list[dict] = []

    def flush() -> None:
        if panel_buf:
            flow.append({"type": "panel", "paras": list(panel_buf), "more": len(panel_buf) - 1})
            panel_buf.clear()

    for p in chapter["paragraphs"]:
        v = view_of(p)
        if p["kind"] == "separator":
            flush()
            flow.append({"type": "sep", "paras": [v]})
        elif "panel_block" in p["flags"]:
            panel_buf.append(v)
        else:
            flush()
            flow.append({"type": "para", "paras": [v]})
    flush()
    return flow


def tally_of(doc: dict) -> dict[str, int]:
    t: dict[str, int] = {}
    for ch in doc["chapters"]:
        for p in ch["paragraphs"]:
            k = tier_of(p["kind"], p["score"])
            t[k] = t.get(k, 0) + 1
    return t


def stats_list(tally: dict[str, int], total: int) -> list[dict]:
    total = total or 1
    return [
        {"label": TIER_LABEL[t], "n": tally.get(t, 0), "pct": f"{tally.get(t, 0) / total:.0%}"}
        for t in ("plot", "mid", "weak", "fade", "zero", "sep")
    ]


def render_doc(doc: dict, out: Path, mode: str = "auto", marks: bool = False) -> str:
    """渲染并把一句话结果返回（run.py 直接打印它）。"""
    chars = sum(len(p["text"]) for ch in doc["chapters"] for p in ch["paragraphs"])
    if mode == "auto":
        mode = "split" if chars > SPLIT_AT_CHARS else "single"
    marks_default = "1" if marks else "0"

    # autoescape 必须打开：正文里偶尔出现 `<` `&`（系统面板、招式名）时，不转义会被
    # 浏览器当标签吃掉，页面和导出都会缺字。模板里唯一不该转义的是内联的导出核心。
    template = Template((ASSETS / "reader.html.j2").read_text(encoding="utf-8"), autoescape=True)
    tally = tally_of(doc)
    total = sum(len(ch["paragraphs"]) for ch in doc["chapters"])
    source = doc.get("source", "")
    stats = stats_list(tally, total)

    if mode == "single":
        chapters = [{**ch, "flow": flow_of(ch)} for ch in doc["chapters"]]
        html = template.render(
            toc=None, chapters=chapters, stats=stats, source=source, nav=None,
            total=total, marks_default=marks_default,
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        return f"{out}  ({len(html) // 1024:,} KB, 单文件)"

    # ---- 分章输出 ----
    outdir = out if out.suffix == "" else out.with_suffix("")
    outdir.mkdir(parents=True, exist_ok=True)

    def name_of(i: int) -> str:
        return f"ch{i:04d}.html"

    toc = []
    for ch in doc["chapters"]:
        flow = flow_of(ch)
        toc.append(
            {
                "index": ch["index"],
                "title": ch["title"] or f"第 {ch['index']} 章",
                "href": name_of(ch["index"]),
                "n": len(ch["paragraphs"]),
                "n_plot": sum(1 for it in flow for p in it["paras"] if p["tier"] == "plot"),
                "n_fade": sum(
                    1 for it in flow for p in it["paras"] if p["tier"] in ("fade", "zero")
                ),
            }
        )

    for i, ch in enumerate(doc["chapters"]):
        nav = {
            "prev": name_of(doc["chapters"][i - 1]["index"]) if i > 0 else None,
            "next": name_of(doc["chapters"][i + 1]["index"])
            if i + 1 < len(doc["chapters"])
            else None,
            "home": "index.html",
            "pos": f"{i + 1} / {len(doc['chapters'])}",
        }
        html = template.render(
            toc=None,
            chapters=[{**ch, "flow": flow_of(ch)}],
            stats=stats,
            source=source,
            nav=nav,
            total=total,
            marks_default=marks_default,
        )
        (outdir / name_of(ch["index"])).write_text(html, encoding="utf-8")

    index = template.render(
        toc=toc, chapters=[], stats=stats, source=source, nav={"home": None},
        total=total, marks_default=marks_default,
    )
    (outdir / "index.html").write_text(index, encoding="utf-8")
    return f"{outdir}/  ({len(doc['chapters'])} 章, 目录 index.html)"


def render_print_html(doc: dict, out: Path, strength: float = 0.55,
                      page_size: str = "A5", start: int = 0, count: int | None = None,
                      title: str = "", keeps=()) -> str:
    """渲染一份"打印用"的静态 HTML：没有控制栏、没有脚本、面板全部展开，
    强度直接烤进 CSS 变量。给无头浏览器打印成 PDF 用（pack_pdf.py）。

    start / count 按**位置**取章节切片（章号可能重复，位置才可靠）。
    """
    chapters = doc["chapters"][start:] if count is None else doc["chapters"][start:start + count]
    if not chapters:
        raise ValueError(f"没有可渲染的章节：start={start} count={count}")

    template = Template((ASSETS / "reader.html.j2").read_text(encoding="utf-8"), autoescape=True)
    rendered = [{**ch, "flow": flow_of(ch)} for ch in chapters]
    tally = tally_of({"chapters": chapters})
    total = sum(len(ch["paragraphs"]) for ch in chapters)
    first = chapters[0]["title"] or f"第 {chapters[0]['index']} 章"
    last = chapters[-1]["title"] or f"第 {chapters[-1]['index']} 章"
    head = title or (f"{doc.get('source', '')} · {first}" + (f" … {last}" if len(chapters) > 1 else ""))
    keep_classes = [k for k in keeps if k in KEEP_KEYS]

    html = template.render(
        toc=None, chapters=rendered, stats=stats_list(tally, total),
        source=doc.get("source", ""), nav=None, total=total,
        marks_default="0", print_mode=True,
        print_strength=f"{max(0.0, min(1.0, strength)):.2f}",
        print_page_size=page_size, print_title=head, keep_classes=keep_classes,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return f"{out}  ({len(chapters)} 章 / {len(html) // 1024:,} KB)"


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: render.py <analyzed.json> [-o out] [--mode auto|single|split] [--marks]")
        raise SystemExit(2)

    src = Path(sys.argv[1])
    out = Path(sys.argv[sys.argv.index("-o") + 1]) if "-o" in sys.argv else src.with_suffix(".html")
    mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "auto"

    doc = json.loads(src.read_text(encoding="utf-8"))
    print("wrote " + render_doc(doc, out, mode=mode, marks="--marks" in sys.argv))


if __name__ == "__main__":
    main()
