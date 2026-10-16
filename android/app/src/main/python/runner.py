"""B 方案在 Android 上的进程内入口。

桌面版 app/engine.py 只通过 subprocess 调 run.py / render.py / pack_epub.py；
Android 没有 subprocess，所以这里把同一批只读模块 import 进来，
参数顺序与 run.py 的 doctor / build / export 完全一致——
判定规则永远只有引擎那一份，Android 侧不存在第二套。

产物一律写进调用方给的 out_dir：Android 的当前目录是只读的，不能相对落盘。
"""

from __future__ import annotations

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
    """选中的文件 -> 引擎认的 src.txt。返回 {"kind", "chapters"} 只给界面回话。

    引擎只吃 TXT（EPUB / HTML 要先抽成文本再进来），所以 EPUB 得先按书脊解成章节
    分明的正文。这层照搬网页端 server.py 的做法，用的是同一个 bookread，Android 侧
    不另起一套解书逻辑。TXT 一个字节都不碰，改名过去就是。
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
        return {"kind": "epub", "chapters": len(book.get("chapters", []))}
    os.replace(raw_path, src)
    return {"kind": "txt", "chapters": 0}


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


def ai_review(src_dir, endpoint, model, key, lo=0.30, hi=0.70, cap=400, batch=16,
              progress_path="", progress=None) -> dict:
    """对已判定的 analyzed.json 做 AI 复核（只碰模糊带），再按 build 的同一套模式重渲。

    复核改的是 analyzed.json，HTML 得跟着重来一次。这一层是必经的：没配 key 就是没配好，
    直接报错；某一批没跑成也不静默退回纯规则——那样等于悄悄少判一段。
    progress_path 给一个普通路径，复核边跑边把 {done,total,reviewed,failed} 写进去，
    界面靠轮询它显示进度——ai_layer 只认 WNF_PROGRESS 这个环境变量，这里替它设上。
    """
    src_dir = Path(src_dir)
    analyzed = src_dir / "analyzed.json"
    if not key:
        raise ValueError("AI 复核是必须的一层：接口地址、模型名和 Key 都得先配好。")

    import ai_layer  # noqa: PLC0415
    ai_layer.ENGINE = _HERE
    os.environ["WNF_PROGRESS"] = str(progress_path or (src_dir / "progress.json"))

    _emit(progress, "AI 复核模糊带…")
    stats = ai_layer.run(analyzed, endpoint, model, key, float(lo), float(hi),
                         int(cap), int(batch), False)
    if stats.get("failed"):
        raise RuntimeError(
            f"AI 复核有 {stats['failed']} 批没跑成（网络或额度的问题）；"
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
