"""B 方案在 Android 上的进程内入口。

桌面版 app/engine.py 只通过 subprocess 调 run.py / render.py / pack_epub.py；
Android 没有 subprocess，所以这里把同一批只读模块 import 进来，
参数顺序与 run.py 的 doctor / build / export 完全一致——
判定规则永远只有引擎那一份，Android 侧不存在第二套。

产物一律写进调用方给的 out_dir：Android 的当前目录是只读的，不能相对落盘。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent

SCRIPTS = _HERE / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import bookread  # noqa: E402
import detect_rules  # noqa: E402
import pack_epub  # noqa: E402
import parse as parse_mod  # noqa: E402
import prescan as prescan_mod  # noqa: E402
import render as render_mod  # noqa: E402
import report as report_mod  # noqa: E402
from tiers import tier_of  # noqa: E402

PRESET_STRENGTH = {"original": 0.0, "balance": 0.55, "plotonly": 1.0}


def tally_of(doc: dict) -> dict[str, int]:
    t: dict[str, int] = {}
    for ch in doc["chapters"]:
        for p in ch["paragraphs"]:
            k = tier_of(p["kind"], p["score"])
            t[k] = t.get(k, 0) + 1
    return t


def _write_json(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def prescan(src, out_dir) -> dict:
    """源文件体检（防盗版反义词替换、`??`、章号异常）。"""
    src, out_dir = Path(src), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = prescan_mod.scan_file(src)
    _write_json(out_dir / "prescan.json", doc)
    (out_dir / "prescan.txt").write_text(prescan_mod.format_report(doc), encoding="utf-8")
    return doc


def prepare_source(raw_path, out_dir, name="") -> dict:
    """选中的文件 -> 引擎认的 src.txt。返回 {"kind","chapters","source_hash"}。

    引擎只吃 TXT（EPUB / HTML 要先抽成文本再进来），所以 EPUB 得先按书脊解成章节
    分明的正文。这层照搬网页端 server.py 的做法，用的是同一个 bookread，Android 侧
    不另起一套解书逻辑。TXT 一个字节都不碰，改名过去就是。

    source_hash 是 src.txt 的内容指纹，供书架按内容去重（同一本书再选一次不再入架）。
    """
    raw_path, out_dir = Path(raw_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    src = out_dir / "src.txt"
    with raw_path.open("rb") as fh:
        head = fh.read(2)
    # 认法跟 bookread.read_book 一致：扩展名或 zip 文件头。
    if (name or raw_path.name).lower().endswith(".epub") or head == b"PK":
        book = bookread.read_book(raw_path.read_bytes(), name)
        src.write_bytes(bookread.to_txt(book).encode("utf-8"))
        raw_path.unlink(missing_ok=True)
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        return {"kind": "epub", "chapters": len(book.get("chapters", [])),
                "source_hash": digest}
    os.replace(raw_path, src)
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    return {"kind": "txt", "chapters": 0, "source_hash": digest}


def _plain_para(tier: str, text: str) -> dict:
    """模板里 macro para(p) 认的那几个字段。直接读没有判定，kind / flags / tip 都空着。"""
    return {"tier": tier or "plain", "kind": "", "flags_attr": "", "tip": "",
            "tier_label": render_mod.TIER_LABEL.get(tier, ""), "text": text}


def _direct_chapters(book: dict) -> list[dict]:
    """把解出来的章编成模板认的 flow。没判过的就是一片 plain，不套任何淡化。"""
    out = []
    for i, ch in enumerate(book.get("chapters", [])):
        tiers = ch.get("tiers") or []
        flow = [
            {"type": "para",
             "paras": [_plain_para(tiers[j] if j < len(tiers) else "", text)]}
            for j, text in enumerate(ch.get("paras", [])) if text.strip()
        ]
        out.append({"index": i + 1, "title": ch.get("title") or f"第 {i + 1} 章", "flow": flow})
    return out


def _direct_template():
    return render_mod.Template(
        (render_mod.ASSETS / "reader.html.j2").read_text(encoding="utf-8"), autoescape=True)


def _hide_bar(html: str) -> str:
    """直接读没有判定，引擎那条控制栏（强度 / 保留原文 / 面板）在这页没有意义，收起来。

    这条排在 </body> 之前、<head> 那份样式之后，同特异度下按顺序取胜。
    """
    return html.replace("</body>", "<style>.bar{display: none}</style>\n</body>", 1)


def _render_direct(book: dict, source: str, out_dir: Path, chars: int) -> Path:
    """按引擎的 reader.html.j2 渲染。大书跟 build 一样拆成每章一份 + 目录 index.html——

    单文件那版 300 万字要到十几 MB、十万个 DOM 节点，手机上打开基本就是卡死；
    拆开每章十来 KB，翻页瞬时。阈值直接用引擎那一个，不另立。
    """
    template = _direct_template()
    views = _direct_chapters(book)
    total = sum(len(c["flow"]) for c in views)

    if chars <= render_mod.SPLIT_AT_CHARS:
        html = template.render(
            toc=None, chapters=views, stats=[], source=source, nav=None,
            total=total, marks_default="0",
        )
        out = out_dir / "book.html"
        out.write_text(_hide_bar(html), encoding="utf-8")
        return out

    def name_of(i: int) -> str:
        return f"ch{i:04d}.html"

    toc = [
        {
            "index": ch["index"],
            "title": ch["title"],
            "href": name_of(ch["index"]),
            "n": sum(len(it["paras"]) for it in ch["flow"]),
            "n_plot": sum(1 for it in ch["flow"] for p in it["paras"] if p["tier"] == "plot"),
            "n_fade": sum(
                1 for it in ch["flow"] for p in it["paras"] if p["tier"] in ("fade", "zero")
            ),
        }
        for ch in views
    ]
    for i, ch in enumerate(views):
        nav = {
            "prev": name_of(views[i - 1]["index"]) if i > 0 else None,
            "next": name_of(views[i + 1]["index"]) if i + 1 < len(views) else None,
            "home": "index.html",
            "pos": f"{i + 1} / {len(views)}",
        }
        html = template.render(
            toc=None, chapters=[ch], stats=[], source=source, nav=nav,
            total=total, marks_default="0",
        )
        (out_dir / name_of(ch["index"])).write_text(_hide_bar(html), encoding="utf-8")

    index = template.render(
        toc=toc, chapters=[], stats=[], source=source, nav={"home": None},
        total=total, marks_default="0",
    )
    index_out = out_dir / "index.html"
    index_out.write_text(_hide_bar(index), encoding="utf-8")
    return index_out


def read_direct(raw_path, out_dir, name="") -> dict:
    """直接读：不判定，解成章节后渲染出来，选完就能翻着看。

    走的是桌面端 /read 那条路——reader.html 拿到文件直接渲染，不经体检、抽查、判定。
    分层照旧保留：EPUB 里本来就带着的深浅（bookread 反解出的 tiers）一并渲染出来；
    没判过的就是一片普通墨色（data-tier="plain" 不套任何淡化）。
    """
    raw_path, out_dir = Path(raw_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    book = bookread.read_book(raw_path.read_bytes(), name or raw_path.name)
    source = book.get("title") or name or raw_path.name
    target = _render_direct(book, source, out_dir, book.get("chars", 0))

    tally: dict[str, int] = {}
    for ch in book.get("chapters", []):
        for t in ch.get("tiers", []):
            if t and t != "plain":
                tally[t] = tally.get(t, 0) + 1
    return {
        "kind": book.get("kind", "txt"),
        "chapters": len(book.get("chapters", [])),
        "chars": book.get("chars", 0),
        "target": str(target),
        "tiers": tally,
    }


def _tally_tiers(book: dict) -> dict[str, int]:
    """数一遍这本书里非「普通/无」的分层档位，给界面显示用。"""
    tally: dict[str, int] = {}
    for ch in book.get("chapters", []):
        for t in ch.get("tiers", []):
            if t and t != "plain":
                tally[t] = tally.get(t, 0) + 1
    return tally


def open_direct(raw_path, book_dir, name="") -> dict:
    """阅读器里直接读一本：解章 + 渲染，同时把它落成一本书（src.txt 留在 book_dir）。

    跟 read_direct 的差别只有一处：read_direct 是纯读、不落盘（工作台那条临时预览）；
    这里的书要留在书架上，所以 source（src.txt）也写下来、并回报内容指纹 source_hash，
    供书架按内容去重。渲染照旧保留 EPUB 里本来就带着的深浅（bookread 反解的 tiers）。
    """
    raw_path, book_dir = Path(raw_path), Path(book_dir)
    book_dir.mkdir(parents=True, exist_ok=True)
    book = bookread.read_book(raw_path.read_bytes(), name or raw_path.name)
    # 复用工作台那条：TXT 原样搬、EPUB 按书脊摊平，顺带把内容指纹算出来。
    info = prepare_source(raw_path, book_dir, name)
    source = book.get("title") or name or raw_path.name
    out = book_dir / "out"
    out.mkdir(parents=True, exist_ok=True)
    target = _render_direct(book, source, out, book.get("chars", 0))
    return {
        "kind": info["kind"],
        "chapters": len(book.get("chapters", [])),
        "chars": book.get("chars", 0),
        "target": str(target),
        "tiers": _tally_tiers(book),
        "source_hash": info["source_hash"],
    }


def source_meta(src_path, name="") -> dict:
    """只解不渲：拿一本书的章数 / 字数 / 分层，用来在阅读器页顶上显示概要。

    从书架续读一本「未判定」的书时，正文页已经渲染好了、不必重渲；这里只读一遍
    src.txt 把概要拿回来，省掉一次整本渲染。

    `src_path` 已经是摊平后的 TXT。`name` 只是原始显示名，可能还带 `.epub`；
    不能拿它判断格式，否则会把 TXT 当 ZIP 解。保留参数仅为兼容旧调用。
    """
    p = Path(src_path)
    book = bookread.read_book(p.read_bytes(), p.name)
    return {"kind": book.get("kind", "txt"),
            "chapters": len(book.get("chapters", [])),
            "chars": book.get("chars", 0),
            "tiers": _tally_tiers(book)}


def _emit(progress, msg: str) -> None:
    """把进度推给壳侧。Android 传进来的是个 Java 对象，调它的 onStep(text)。

    这纯粹是给界面看的，失败绝不能拖累流水线——所以吞掉一切异常。
    """
    if progress is None:
        return
    try:
        progress.onStep(msg)
    except Exception:  # noqa: BLE001
        pass


def build(src, out_dir, limit=None, start_at=1, marks=False, mode="auto",
          want_report=True, progress=None) -> dict:
    """parse -> detect -> report -> render。返回摘要：章数 / 字数 / 分层 / 产物路径。

    progress 是可选的回调对象，每跨过一段就回调一次——大书里 detect 那一段要跑很久，
    界面上不给点动静用户会以为死机。
    """
    src, out_dir = Path(src), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log: list[str] = []

    _emit(progress, "解析正文…")
    t0 = time.perf_counter()
    doc = parse_mod.parse_file(src, limit=limit, start_at=start_at)
    chars = sum(len(p["text"]) for ch in doc["chapters"] for p in ch["paragraphs"])
    _write_json(out_dir / "parsed.json", doc)
    log.append(f"[1/4] parse   {len(doc['chapters'])} 章 / {chars:,} 字  ({time.perf_counter() - t0:.1f}s)")
    _emit(progress, f"[1/4] 解析完成：{len(doc['chapters'])} 章 / {chars:,} 字")

    _emit(progress, "判定分层…")
    t0 = time.perf_counter()
    doc = detect_rules.analyze(doc)
    _write_json(out_dir / "analyzed.json", doc)
    log.append(f"[2/4] detect 人名候选 {len(doc['names'])}  ({time.perf_counter() - t0:.1f}s)")
    _emit(progress, f"[2/4] 判定完成：{len(doc['chapters'])} 章 / 人名候选 {len(doc['names'])}")

    _emit(progress, "生成逐段对照表…")
    t0 = time.perf_counter()
    if want_report:
        (out_dir / "report.txt").write_text(report_mod.render_report(doc), encoding="utf-8")
    log.append(f"[3/4] report ({time.perf_counter() - t0:.1f}s)")
    _emit(progress, "[3/4] 对照表完成")

    _emit(progress, "渲染阅读版…")
    t0 = time.perf_counter()
    real = mode if mode != "auto" else (
        "split" if chars > render_mod.SPLIT_AT_CHARS else "single"
    )
    target = out_dir if real == "split" else out_dir / "book.html"
    detail = render_mod.render_doc(doc, target, mode=real, marks=marks)
    log.append(f"[4/4] render {detail}  ({time.perf_counter() - t0:.1f}s)")
    _emit(progress, f"[4/4] 渲染完成：{len(doc['chapters'])} 章")

    return {
        "chapters": len(doc["chapters"]),
        "chars": chars,
        "names": len(doc["names"]),
        "tiers": tally_of(doc),
        "target": str(target),
        "log": log,
    }


def export_epub(src_dir, out_path, preset="balance", lever="color",
                strength=None, keeps=(), panel="full") -> dict:
    """把 analyzed.json 打成 EPUB（run.py export 的进程内版，只留 epub 分支）。"""
    doc = json.loads((Path(src_dir) / "analyzed.json").read_text(encoding="utf-8"))
    s = PRESET_STRENGTH[preset] if strength is None else float(strength)
    info = pack_epub.pack_epub(doc, Path(out_path), strength=s, keeps=list(keeps),
                               panel=panel, lever=lever)
    return {"path": str(info["path"]), "bytes": info["bytes"],
            "chapters": info["chapters"], "graded": info["graded"], "strength": s}


def ai_probe(endpoint, model, key) -> dict:
    """测一次连接：地址 / Key / 模型能不能用。不碰任何文件。"""
    import ai_layer  # noqa: PLC0415
    # 打包后 ai_layer.py 落在 Chaquopy 源根，引擎也整份平铺在同一个目录下，
    # 桌面上那套 parent.parent/"engine"/"webnovel-focus" 在这里指不到地方。
    ai_layer.ENGINE = _HERE
    ok, msg = ai_layer.probe(endpoint, model, key)
    return {"ok": ok, "message": msg}


def ai_review(src_dir, endpoint, model, key, lo=0.30, hi=0.70, cap=800, batch=16,
              progress_path="", progress=None, concurrency=3) -> dict:
    """对已判定的 analyzed.json 做 AI 标定和定向升级，再按同一套模式重渲。

    复核改的是 analyzed.json，HTML 得跟着重来一次。这一层是必经的：没配 key 就是没配好，
    直接报错；某一批没跑成也不静默退回纯规则。旧参数 lo/hi 保留只为协议兼容，
    cap/AI_MAX 现在解释为抽样 + 升级的总预算。
    progress_path 给一个普通路径，复核边跑边把进度写进去，
    界面靠轮询它显示进度——ai_layer 只认 WNF_PROGRESS 这个环境变量，这里替它设上。
    """
    src_dir = Path(src_dir)
    analyzed = src_dir / "analyzed.json"
    if not key:
        raise ValueError("AI 标定是必须的一层：接口地址、模型名和 Key 都得先配好。")

    import ai_layer  # noqa: PLC0415
    ai_layer.ENGINE = _HERE
    os.environ["WNF_PROGRESS"] = str(progress_path or (src_dir / "progress.json"))

    _emit(progress, "AI 抽样标定…")
    # 桌面端新口径：cap/AI_MAX 是抽样 + 定向升级的总预算；lo/hi 只留作兼容。
    stats = ai_layer.run(analyzed, endpoint, model, key,
                         budget=int(cap), seed=0, batch=int(batch),
                         concurrency=int(concurrency), dry=False)
    if stats.get("failed"):
        raise RuntimeError(
            f"AI 标定有 {stats['failed']} 批没跑成（网络或额度的问题）；"
            "确认接口能用之后再出整本。")
    _emit(progress, f"复核完成：改判 {stats.get('reviewed', 0)} 段 / "
                    f"失败 {stats.get('failed', 0)} 批")
    _emit(progress, "按复核结果重渲阅读版…")

    doc = json.loads(analyzed.read_text(encoding="utf-8"))
    chars = sum(len(p["text"]) for ch in doc["chapters"] for p in ch["paragraphs"])
    real = "split" if chars > render_mod.SPLIT_AT_CHARS else "single"
    target = src_dir if real == "split" else src_dir / "book.html"
    render_mod.render_doc(doc, target, mode=real, marks=False)
    _emit(progress, "阅读版已按复核结果更新")

    # 只有 AI 全流程成功、analyzed.json 已应用且阅读版重渲完成后才落这个标记。
    # 仅看 analyzed.json 或 calibration.json 都不够：失败批次的标定文件也可能已写出。
    marker = src_dir / "ai_ok.json"
    marker.write_text(json.dumps({
        "version": 1,
        "analyzed_sha256": hashlib.sha256(analyzed.read_bytes()).hexdigest(),
        "model": model,
        "reviewed": stats.get("reviewed", 0),
        "failed": 0,
        "calibration": str(stats.get("calibration") or ""),
    }, ensure_ascii=False), encoding="utf-8")

    return {
        "asked": stats.get("asked", 0), "chars": stats.get("chars", 0),
        "batches": stats.get("batches", 0), "reviewed": stats.get("reviewed", 0),
        "failed": stats.get("failed", 0), "target": str(target),
    }


def read_analyzed(src_dir) -> dict:
    """复核改的是 analyzed.json，界面上的章数 / 字数 / 分层得读复核后的那份。

    形状与 build 的返回一致（chapters / chars / names / tiers），界面直接拿去用。
    """
    doc = json.loads((Path(src_dir) / "analyzed.json").read_text(encoding="utf-8"))
    chars = sum(len(p["text"]) for ch in doc["chapters"] for p in ch["paragraphs"])
    return {"chapters": len(doc["chapters"]), "chars": chars,
            "names": len(doc.get("names", [])), "tiers": tally_of(doc)}


def doctor(src, out_dir, chapters=3, start_at=1) -> dict:
    """体检 + 前几章抽查页（引擎强制的确认步骤，整本 build 之前必须先过）。"""
    src, out_dir = Path(src), Path(out_dir)
    scan = prescan(src, out_dir)
    res = build(src, out_dir / "confirm", limit=chapters, start_at=start_at,
                marks=True, mode="single", want_report=True)
    res["verdict"] = scan["verdict"]
    return res


def probe() -> dict:
    """自检：报告 App 侧的 Python / 依赖 / 引擎可见性。"""
    info: dict = {"python": sys.version.split()[0], "home": os.environ.get("HOME", ""),
                  "here": str(_HERE), "extracted": _HERE.is_dir()}
    try:
        import jinja2
        info["jinja2"] = getattr(jinja2, "__version__", "?")
    except Exception as exc:  # noqa: BLE001
        info["jinja2"] = f"FAIL {exc}"
    try:
        import sklearn
        info["sklearn"] = getattr(sklearn, "__version__", "?")
    except Exception as exc:  # noqa: BLE001
        info["sklearn"] = f"FAIL {exc}"
    try:
        info["template"] = str(render_mod.ASSETS / "reader.html.j2")
        info["template_exists"] = (render_mod.ASSETS / "reader.html.j2").is_file()
    except Exception as exc:  # noqa: BLE001
        info["template"] = f"FAIL {exc}"
    return info
