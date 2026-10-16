"""一条命令串起 parse -> detect -> report -> render。

三个子命令对应三段真实工作：

  doctor  新书第一次接触。先体检源文件（防盗版反义词替换、`??`、章号异常），
          再报出这本书的风格基线，最后把前几章连判定标记一起渲染出来，
          交给用户确认判定是否合理。确认之前不要开始读整本。
  build   正式出阅读版：解析 -> 判定 -> 对照表 -> 渲染。大于 40 万字自动分章。
  sample  从某一层抽样审查，人工判断规则是否合理（改规则前后用同一种子对比）。
  export  把判定结果打成能给读者带走的载体：EPUB（真·淡化，推荐）或 PDF。

用法:
    python run.py doctor <书.txt> [--chapters 3] [--from 1] [-o build/<书名>]
    python run.py build  <书.txt> [--limit N] [--from N] [--mode auto|single|split]
                                  [--no-report] [--skip-check] [-o build/<书名>]
    python run.py sample <analyzed.json> [--tier plot] [--n 25] [--chapter N]
    python run.py export <analyzed.json> [--format epub|pdf] [--preset balance|original|plotonly]
                                  [--strength S] [--keep sys_panel,scenery] [--panel full|collapse]
                                  [--lever color|opacity] [--page-size A5] [--chapters-per-file N]
                                  [--from N] [--to M] [-o out]

产物都落在 -o 目录下（默认 build/<文件名去后缀>/）：
    parsed.json   段落 + 字符偏移
    analyzed.json 判定结果（区间 / 类型 / 证据 / 分数）
    report.txt    逐段对照表，用来核对判定
    book.html     单文件阅读版（节选）
    index.html    分章阅读版的目录页（全书）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_rules  # noqa: E402
import eval as eval_mod  # noqa: E402
import pack_epub as epub_mod  # noqa: E402
import pack_pdf as pdf_mod  # noqa: E402
import parse as parse_mod  # noqa: E402
import prescan as prescan_mod  # noqa: E402
import render as render_mod  # noqa: E402
import report as report_mod  # noqa: E402
from tiers import tier_of  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

TIER_LABEL = {"plot": "情节", "mid": "普通", "weak": "偏水", "fade": "注水", "zero": "零信息", "sep": "分隔"}


def step(n: int, total: int, label: str, detail: str, seconds: float) -> str:
    return f"[{n}/{total}] {label:<6} {detail}  ({seconds:.1f}s)"


def tally_of(doc: dict) -> dict[str, int]:
    t: dict[str, int] = {}
    for ch in doc["chapters"]:
        for p in ch["paragraphs"]:
            k = tier_of(p["kind"], p["score"])
            t[k] = t.get(k, 0) + 1
    return t


def tier_line(tally: dict[str, int]) -> str:
    total = sum(tally.values()) or 1
    order = ("plot", "mid", "weak", "fade", "zero", "sep")
    return " · ".join(
        f"{TIER_LABEL[k]} {tally.get(k, 0) / total:.0%}" for k in order if tally.get(k)
    )


def kind_line(doc: dict) -> str:
    counts: dict[str, int] = {}
    for ch in doc["chapters"]:
        for p in ch["paragraphs"]:
            counts[p["kind"]] = counts.get(p["kind"], 0) + 1
    want = (("author_note", "作者话"), ("sys_panel", "系统面板"), ("watermark", "水印/广告"),
            ("punctuation", "标点连打"), ("separator", "分隔行"))
    got = [f"{label} {counts[k]}" for k, label in want if counts.get(k)]
    return " · ".join(got) or "无硬规则命中"


def do_prescan(src: Path, out_dir: Path) -> dict:
    t0 = time.perf_counter()
    doc = prescan_mod.scan_file(src)
    (out_dir / "prescan.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    text = prescan_mod.format_report(doc)
    (out_dir / "prescan.txt").write_text(text, encoding="utf-8")
    doc["_seconds"] = time.perf_counter() - t0
    return doc


def build_pipeline(src: Path, out_dir: Path, limit: int | None, start_at: int,
                   log: list[str], marks: bool = False, mode: str = "auto",
                   want_report: bool = True, label: str = "") -> tuple[dict, Path]:
    t0 = time.perf_counter()
    doc = parse_mod.parse_file(src, limit=limit, start_at=start_at)
    chars = sum(len(p["text"]) for ch in doc["chapters"] for p in ch["paragraphs"])
    (out_dir / "parsed.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8"
    )
    log.append(
        step(1, 4, "parse", f"{len(doc['chapters'])} 章 / {chars:,} 字 -> parsed.json",
             time.perf_counter() - t0)
    )

    t0 = time.perf_counter()
    doc = detect_rules.analyze(doc)
    (out_dir / "analyzed.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8"
    )
    log.append(
        step(2, 4, "detect", f"人名候选 {len(doc['names'])} · {kind_line(doc)} -> analyzed.json",
             time.perf_counter() - t0)
    )

    tally = tally_of(doc)
    if want_report:
        t0 = time.perf_counter()
        (out_dir / "report.txt").write_text(
            report_mod.render_report(doc), encoding="utf-8"
        )
        log.append(
            step(3, 4, "report", f"{tier_line(tally)} -> report.txt",
                 time.perf_counter() - t0)
        )
    else:
        log.append(step(3, 4, "report", "跳过（--no-report）", 0.0))

    t0 = time.perf_counter()
    real_mode = mode if mode != "auto" else (
        "split" if chars > render_mod.SPLIT_AT_CHARS else "single"
    )
    target = out_dir if real_mode == "split" else out_dir / "book.html"
    detail = render_mod.render_doc(doc, target, mode=real_mode, marks=marks)
    log.append(step(4, 4, "render", detail, time.perf_counter() - t0))
    return doc, target


def cmd_doctor(args: argparse.Namespace) -> int:
    src = Path(args.src)
    out_dir = Path(args.out) if args.out else Path("build") / src.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"# 新书首次使用 · {src.name}\n")
    scan = do_prescan(src, out_dir)
    print(prescan_mod.format_report(scan))
    print(f"\n(wrote {out_dir / 'prescan.txt'}, {out_dir / 'prescan.json'})")

    confirm_dir = out_dir / "confirm"
    confirm_dir.mkdir(parents=True, exist_ok=True)

    log: list[str] = []
    doc, target = build_pipeline(
        src, confirm_dir, limit=args.chapters, start_at=args.start,
        log=log, marks=True, mode="single", want_report=True,
    )
    print("\n--- 前几章抽查（渲染时判定标记默认打开）---")
    print("\n".join(log))
    tally = tally_of(doc)
    print(f"\n抽查范围：第 {doc['chapters'][0]['index']} 章起 {len(doc['chapters'])} 章")
    print(f"分层：{tier_line(tally)}")
    print(f"硬规则命中：{kind_line(doc)}")
    print(f"人名候选 {len(doc['names'])} 个：{'、'.join(doc['names'][:12]) or '(无)'}")
    print(f"抽查页：{target}")

    print("\n--- 接下来要问用户的三件事 ---")
    print("1. 判定是否合理：打开抽查页，「显示判定」已默认打开，鼠标停在段落上看判定证据；")
    print("   分层不对的段落点一下可以永久保留原文。哪一类判错了要说清是哪一类（写景/面板/对话/短句）。")
    print("2. 口味：默认「平衡」。只想看剧情就调到「只看剧情」，恋战的题材保持默认。")
    print("3. 源文件问题（若有）怎么处理：读干净部分还是照读残文，换源要不要等用户。")
    print("\n确认之后再跑 build 出整本。")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    src = Path(args.src)
    out_dir = Path(args.out) if args.out else Path("build") / src.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_check:
        scan = do_prescan(src, out_dir)
        issues = scan["verdict"]["notes"]
        head = "源文件体检通过" if not issues else f"源文件体检 {len(issues)} 条要注意：{issues[0]}"
        print(f"[check] {head}")
        if issues:
            print(f"        （完整体检单：{out_dir / 'prescan.txt'}）")

    log: list[str] = []
    doc, target = build_pipeline(
        src, out_dir, limit=args.limit, start_at=args.start,
        log=log, marks=False, mode=args.mode, want_report=not args.no_report,
    )
    print("\n".join(log))

    chars = sum(len(p["text"]) for ch in doc["chapters"] for p in ch["paragraphs"])
    print(f"\n范围：第 {doc['chapters'][0]['index']}–{doc['chapters'][-1]['index']} 章 / {chars:,} 字")
    print(f"分层：{tier_line(tally_of(doc))}")
    print(f"交付：{target}")
    print("偏好不用重新跑引擎：页面顶栏能实时调（强度/分组保留/面板处置），翻页会把偏好带过去。")
    return 0


def cmd_sample(args: argparse.Namespace) -> int:
    doc = json.loads(Path(args.src).read_text(encoding="utf-8"))
    text = eval_mod.sample(
        doc, tier=args.tier, n=args.n, seed=args.seed,
        context=args.context, chapter=args.chapter,
    )
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


PRESET_STRENGTH = {"original": 0.0, "balance": 0.55, "plotonly": 1.0}


def cmd_export(args: argparse.Namespace) -> int:
    """把 analyzed.json 打成 EPUB / PDF。"""
    src = Path(args.src)
    strength = args.strength if args.strength is not None else PRESET_STRENGTH[args.preset]
    keeps = [k for k in (args.keep or "").split(",") if k]

    doc = json.loads(src.read_text(encoding="utf-8"))
    stem = Path(doc.get("source") or src.stem).stem
    span = {"start": max(0, args.start), "count": None if args.to is None else max(0, args.to - args.start)}

    if args.format == "epub":
        out = Path(args.out) if args.out else src.parent / f"{stem}.epub"
        info = epub_mod.pack_epub(doc, out, strength=strength, keeps=keeps,
                                  panel=args.panel, lever=args.lever, **span)
        print(f"wrote {info['path']}  ({info['bytes'] / 1024:,.0f} KB / {info['chapters']} 章 / "
              f"{info['graded']:,} 段带分层样式)")
        print(f"强度 {strength:.2f} · 淡化杠杆 {info['lever']} · 面板 {args.panel} · "
              f"保留原文 {','.join(keeps) or '无'}")
        print("导入阅读器看效果。各家保留的样式不一样：如果灰色没生效，"
              "用 --lever opacity 再打一份；拿不准就先跑 experiments/make_css_probe.py "
              "生成样式探针导进去看。")
    else:
        out = Path(args.out) if args.out else src.parent / f"{stem}.pdf"
        info = pdf_mod.pack_pdf(doc, out, strength=strength, page_size=args.page_size,
                                per_file=args.chapters_per_file, marks=False, keeps=keeps, **span)
        for f in info["files"]:
            pages = f"{f['pages']} 页" if f["pages"] else "页数未知"
            print(f"wrote {f['path']}  ({f['bytes'] / 1024:,.0f} KB / {f['chapters']} 章 / {pages})")
        print(f"强度 {strength:.2f} · 纸张 {info['page_size']} · 面板始终展开（打印无法折叠）")
        print("打印用的 HTML 留在同目录（*.print.html）：想调页边距就用浏览器打开它、"
              "「打印 → 另存为 PDF」，不必重跑。")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="run.py", description="网文分级渲染：一条命令串起整条流水线")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="新书第一次接触：体检 + 基线 + 前几章抽查")
    d.add_argument("src")
    d.add_argument("--chapters", type=int, default=3, help="抽查几章（默认 3）")
    d.add_argument("--from", dest="start", type=int, default=1, help="从第几章开始抽查")
    d.add_argument("-o", dest="out", default=None)
    d.set_defaults(func=cmd_doctor)

    b = sub.add_parser("build", help="正式出阅读版")
    b.add_argument("src")
    b.add_argument("--limit", type=int, default=None, help="只处理前 N 章")
    b.add_argument("--from", dest="start", type=int, default=1, help="从第 N 章开始")
    b.add_argument("--mode", choices=("auto", "single", "split"), default="auto")
    b.add_argument("--no-report", action="store_true", help="不写 report.txt（大书上省时间）")
    b.add_argument("--skip-check", action="store_true", help="跳过源文件体检")
    b.add_argument("-o", dest="out", default=None)
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("sample", help="从某一层抽样审查判定")
    s.add_argument("src")
    s.add_argument("--tier", default="plot", choices=("plot", "mid", "weak", "fade", "zero"))
    s.add_argument("--n", type=int, default=25)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--context", type=int, default=1)
    s.add_argument("--chapter", type=int, default=0)
    s.add_argument("-o", dest="out", default=None)
    s.set_defaults(func=cmd_sample)

    e = sub.add_parser("export", help="把判定结果打成 EPUB / PDF")
    e.add_argument("src", help="analyzed.json")
    e.add_argument("--format", choices=("epub", "pdf"), default="epub")
    e.add_argument("--preset", choices=("balance", "original", "plotonly"), default="balance")
    e.add_argument("--strength", type=float, default=None,
                   help="直接给 0~1 的强度，覆盖 --preset")
    e.add_argument("--keep", default="",
                   help="保留原文的分组，逗号分隔：sys_panel,scenery,short_burst,recap,dialogue,junk")
    e.add_argument("--panel", choices=("full", "collapse"), default="full",
                   help="EPUB 用：面板全部保留，或只留首行 + 〔略去 N 行〕")
    e.add_argument("--lever", choices=epub_mod.LEVERS, default="color",
                   help="EPUB 用哪个 CSS 杠杆表达淡化：color=灰色（兼容最好）/ opacity=透明度")
    e.add_argument("--page-size", choices=pdf_mod.PAGE_SIZES, default="A5")
    e.add_argument("--chapters-per-file", type=int, default=0,
                   help="PDF 用：每 N 章一个 PDF（大书必用，0 = 全部塞一个）")
    e.add_argument("--from", dest="start", type=int, default=0, help="按位置，从第几章开始")
    e.add_argument("--to", dest="to", type=int, default=None, help="按位置，到第几章为止（不含）")
    e.add_argument("-o", dest="out", default=None)
    e.set_defaults(func=cmd_export)

    args = ap.parse_args()
    if not Path(args.src).is_file():
        print(f"找不到输入文件：{args.src}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
