"""零改动包住 webnovel-focus 引擎。

只通过引擎自己的命令行入口调用它（run.py / render.py），从不写入引擎目录。
所有产物与 cwd 都落在项目内 work/，这样引擎默认的相对产物不会漏进技能的安装目录。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 项目根，例：.../Desktop/redline
ENGINE = ROOT / "engine" / "webnovel-focus"            # 引擎只读副本
WORK = ROOT / "work"                                   # 运行时产物（可删）
RUN_PY = ENGINE / "scripts" / "run.py"
RENDER_PY = ENGINE / "scripts" / "render.py"
AI_PY = ROOT / "app" / "ai_layer.py"
PY = sys.executable or "python"

SAFE_NAME = re.compile(r"[^0-9A-Za-z_\-\.\u4e00-\u9fff]+")


def _run(args: list[str], timeout: int = 1800, env: dict | None = None) -> tuple[bool, str]:
    """在项目内跑引擎脚本，返回 (是否成功, 日志)。cwd 永远指向 work/。"""
    WORK.mkdir(parents=True, exist_ok=True)
    child = os.environ.copy()
    if env:
        child.update(env)
    try:
        proc = subprocess.run(
            [PY, *args],
            cwd=str(WORK), capture_output=True, text=True, env=child,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"[timeout] 超过 {timeout}s 没跑完：{' '.join(args)}"
    log = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, log.strip()


def new_job(original_name: str) -> tuple[str, Path]:
    jid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:5]
    d = WORK / jid
    (d / "out").mkdir(parents=True, exist_ok=True)
    meta = {"job": jid, "name": safe_stem(original_name), "created": time.time()}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return jid, d


def remove_job(d: Path) -> None:
    """删掉这本书的整个工作目录：源文件、抽查页、产物、日志全在里面。"""
    shutil.rmtree(d)


def safe_stem(name: str) -> str:
    stem = Path(name).stem.strip() or "book"
    stem = SAFE_NAME.sub("_", stem)[:48]
    return stem or "book"


def save_source(d: Path, filename: str, data: bytes) -> Path:
    """引擎只吃 TXT。文件名保留原名，否则 EPUB/PDF 的书名会变成 source。"""
    src = d / f"{safe_stem(filename)}.txt"
    src.write_bytes(data)
    meta = read_meta(d)
    meta["src"] = src.name
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return src


def source_of(d: Path) -> Path:
    """工作目录里还有 prescan.txt 之类的产物，别把它们当成书。"""
    name = read_meta(d).get("src")
    if name and (d / name).is_file():
        return d / name
    for f in sorted(d.glob("*.txt")):
        if f.name not in ("prescan.txt", "report.txt"):
            return f
    raise FileNotFoundError("工作目录里找不到源文件")


def out_of(d: Path) -> Path:
    return d / "out"


def reader_of(d: Path) -> Path | None:
    """阅读版在哪个文件：AI 复核重渲过是 index.html，纯规则出的是 book.html。"""
    out = out_of(d)
    for name in ("index.html", "book.html"):
        f = out / name
        if f.is_file():
            return f
    return None


# ---------- 三段真实工作 ----------

def doctor(d: Path, src: Path) -> tuple[bool, str, Path]:
    """体检 + 前几章抽查页（引擎强制的确认步骤，整本 build 之前必须先过这一关）。"""
    ok, log = _run([str(RUN_PY), "doctor", str(src), "--chapters", "3", "--from", "1", "-o", str(d)], 900)
    return ok, log, d / "confirm" / "book.html"


def build(d: Path, src: Path, mode: str = "auto") -> tuple[bool, str]:
    """正式出阅读版：解析 -> 判定 -> 对照表 -> 渲染。"""
    return _run([str(RUN_PY), "build", str(src), "--mode", mode, "-o", str(out_of(d))], 3600)


def render(d: Path) -> tuple[bool, str]:
    """AI 层改过 analyzed.json 之后，用同一份数据重渲一次 HTML。"""
    out = out_of(d)
    return _run([str(RENDER_PY), str(out / "analyzed.json"), "-o", str(out / "index.html"),
                 "--mode", "auto"], 1800)


def export(d: Path, preset: str = "balance", lever: str = "color") -> tuple[bool, str, Path]:
    """只出 EPUB。PDF 那一层要靠本机浏览器无头打印，大书会把进程拖死，已经去掉。"""
    out = out_of(d)
    target = out / f"{read_meta(d)['name']}.epub"
    args = [str(RUN_PY), "export", str(out / "analyzed.json"), "--format", "epub",
            "--preset", preset, "--lever", lever, "-o", str(target)]
    ok, log = _run(args, 1800)
    return ok, log, target


def ai_review(d: Path, endpoint: str, model: str, key: str, lo: float, hi: float,
              cap: int, dry: bool = False, progress: Path | None = None) -> tuple[bool, str]:
    """只把模糊带的段落交给用户自己的模型复核，结果写回 analyzed.json。

    模型用的判定标准来自 skill 自己写的 references/（只读），不是另编一套。
    progress 给一个文件路径，AI 层会边跑边把进度写进去，服务端就能读出实时进度。
    """
    args = [str(AI_PY), str(out_of(d) / "analyzed.json"),
            "--endpoint", endpoint or "https://api.openai.com/v1",
            "--model", model or "gpt-4o-mini",
            "--lo", str(lo), "--hi", str(hi), "--max", str(cap)]
    if dry:
        args.append("--dry")
    env = {"WNF_AI_KEY": key or ""}
    if progress is not None:
        env["WNF_PROGRESS"] = str(progress)
    return _run(args, 3600, env=env)


def read_progress(d: Path) -> dict:
    """AI 复核的实时进度（ai_layer 边跑边写 progress.json）。没跑过就是空。"""
    try:
        return json.loads((d / "progress.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def ai_probe(endpoint: str, model: str, key: str) -> tuple[bool, str]:
    """测一次连接：地址 / Key / 模型能不能用。不碰任何文件。"""
    ok, log = _run([str(AI_PY), "--probe",
                    "--endpoint", endpoint or "https://api.openai.com/v1",
                    "--model", model or "gpt-4o-mini"], 90, env={"WNF_AI_KEY": key or ""})
    try:
        data = json.loads(log.strip().splitlines()[-1])
        return bool(data.get("ok")), str(data.get("message") or "")
    except Exception:
        return False, (log.strip() or "连接测试没有返回结果。")


def skill_rules() -> str:
    """AI 复核层实际喂给模型的判定标准（读自引擎副本，只读）。给页面透明展示用。"""
    ok, log = _run([str(AI_PY), "--show-rules"], 60)
    return log if ok else ""


def read_meta(d: Path) -> dict:
    try:
        return json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        return {"job": d.name, "name": d.name}


def tally(d: Path) -> dict:
    """从 analyzed.json 数一遍分层，给页面显示用。只读，不改。"""
    sys.path.insert(0, str(ENGINE / "scripts"))
    from tiers import tier_of  # noqa: PLC0415

    try:
        doc = json.loads((out_of(d) / "analyzed.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    t: dict[str, int] = {}
    for ch in doc.get("chapters", []):
        for p in ch.get("paragraphs", []):
            k = tier_of(p.get("kind", "body"), p.get("score") or 0.0)
            t[k] = t.get(k, 0) + 1
    return t


def result_of(d: Path) -> dict:
    """页面要的东西：书名、分层、产物地址。"""
    out = out_of(d)
    meta = read_meta(d)
    reader = reader_of(d)
    files = []
    # 只收搬得走的成品。out/ 里还躺着引擎渲染的几百上千个 chXXXX.html 分片，
    # 它们是阅读版的零件、不是给你下载的东西，收进来会把页面撑爆。
    for f in sorted(out.glob("*.epub")):
        if f.is_file():
            files.append({"name": f.name, "url": f"/work/{d.name}/out/{f.name}",
                          "kb": round(f.stat().st_size / 1024)})
    return {
        "job": d.name,
        "name": meta.get("name", d.name),
        "tiers": tally(d),
        "confirm": f"/work/{d.name}/confirm/book.html",
        "reader": f"/work/{d.name}/out/{reader.name}" if reader else "",
        "files": files,
        "prescan": f"/work/{d.name}/prescan.txt",
        "report": f"/work/{d.name}/out/report.txt",
    }
