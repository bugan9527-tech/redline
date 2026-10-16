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


def build(src, out_dir, limit=None, start_at=1, marks=False, mode="auto",
          want_report=True) -> dict:
    """parse -> detect -> report -> render。返回摘要：章数 / 字数 / 分层 / 产物路径。"""
    src, out_dir = Path(src), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log: list[str] = []

    t0 = time.perf_counter()
    doc = parse_mod.parse_file(src, limit=limit, start_at=start_at)
    chars = sum(len(p["text"]) for ch in doc["chapters"] for p in ch["paragraphs"])
    _write_json(out_dir / "parsed.json", doc)
    log.append(f"[1/4] parse   {len(doc['chapters'])} 章 / {chars:,} 字  ({time.perf_counter() - t0:.1f}s)")

    t0 = time.perf_counter()
    doc = detect_rules.analyze(doc)
    _write_json(out_dir / "analyzed.json", doc)
    log.append(f"[2/4] detect 人名候选 {len(doc['names'])}  ({time.perf_counter() - t0:.1f}s)")

    t0 = time.perf_counter()
    if want_report:
        (out_dir / "report.txt").write_text(report_mod.render_report(doc), encoding="utf-8")
    log.append(f"[3/4] report ({time.perf_counter() - t0:.1f}s)")

    t0 = time.perf_counter()
    real = mode if mode != "auto" else (
        "split" if chars > render_mod.SPLIT_AT_CHARS else "single"
    )
    target = out_dir if real == "split" else out_dir / "book.html"
    detail = render_mod.render_doc(doc, target, mode=real, marks=marks)
    log.append(f"[4/4] render {detail}  ({time.perf_counter() - t0:.1f}s)")

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
