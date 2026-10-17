"""零改动包住 webnovel-focus 引擎。

只通过引擎自己的命令行入口调用它（run.py / render.py），从不写入引擎目录。
所有产物与 cwd 都落在项目内 work/，这样引擎默认的相对产物不会漏进技能的安装目录。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
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
_CHAPTER_TITLE = re.compile(
    r"(?:第[0-9零一二三四五六七八九十百千万两]+[章节回卷篇部]"
    r"|Chapter\s+\d+|Part\s+\d+|\d+\s*[、.．])"
)
BOOK_KEY_VERSION = 4

# 正在跑的子进程，按任务号登记。取消任务时靠它把进程掐掉——
# 一次处理最长要跑一小时（三百多万字的大书），不给取消就只能干等。
PROCS: dict[str, subprocess.Popen] = {}
_PLOCK = threading.Lock()


def _kill(proc: subprocess.Popen) -> None:
    """子进程下面还挂着孙子进程（引擎自己也可能再起进程），杀就要杀整棵。"""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=20)
        else:
            proc.kill()
    except Exception:  # noqa: BLE001
        pass


def cancel(job: str) -> bool:
    """掐掉这个任务正在跑的子进程。返回是否真掐到了（没在跑就是 False）。"""
    with _PLOCK:
        proc = PROCS.get(job)
    if proc is None or proc.poll() is not None:
        return False
    _kill(proc)
    return True


def _run(args: list[str], timeout: int = 1800, env: dict | None = None,
         job: str | None = None) -> tuple[bool, str]:
    """在项目内跑引擎脚本，返回 (是否成功, 日志)。cwd 永远指向 work/。

    带 job 就把这个子进程登记下来，服务端的 /api/cancel 才能把它掐掉。
    """
    WORK.mkdir(parents=True, exist_ok=True)
    child = os.environ.copy()
    if env:
        child.update(env)
    try:
        proc = subprocess.Popen(
            [PY, *args],
            cwd=str(WORK), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=child, encoding="utf-8", errors="replace",
        )
    except OSError as exc:  # noqa: BLE001
        return False, f"[error] 进程起不来：{exc}"
    if job:
        with _PLOCK:
            PROCS[job] = proc
    try:
        out, err = proc.communicate(timeout=timeout)
        log = ((out or "") + (err or "")).strip()
        if proc.returncode == 0:
            return True, log
        return False, log or f"[error] 退出码 {proc.returncode}"
    except subprocess.TimeoutExpired:
        _kill(proc)
        proc.communicate()
        return False, f"[timeout] 超过 {timeout}s 没跑完：{' '.join(args)}"
    finally:
        if job:
            with _PLOCK:
                PROCS.pop(job, None)


def kind_of(name: str) -> str:
    """这本是从什么格式进来的。EPUB 也要认——书架上看得出原样。"""
    return "epub" if str(name).lower().endswith(".epub") else "txt"


def _write_meta(d: Path, meta: dict) -> None:
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _norm_identity(text: str) -> str:
    """书身份只认正文，不认空白和排版差异。"""
    return re.sub(r"\s+", "", str(text or ""))


def book_key_of(book: dict, name: str = "") -> str:
    """同一本书跨 TXT / EPUB 的稳定指纹。

    只以正文内容为准：TXT 文件名和 EPUB 元数据标题都不参与身份计算，避免同一
    内容换个文件名或标题就变成两条书架记录。封面页/参数页会被忽略；只有正文为空
    时才退回标题或文件名作为兜底。
    """
    chapters = book.get("chapters") or []
    body = [c for c in chapters if _CHAPTER_TITLE.search(str(c.get("title") or ""))] or chapters
    raw = "\n".join(
        _norm_identity(p)
        for c in body
        for p in (c.get("paras") or [])
    )
    if not raw:
        raw = _norm_identity(book.get("title") or safe_stem(name))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_job(original_name: str, *, book_key: str = "", source_hash: str = "",
            kind: str = "") -> tuple[str, Path]:
    jid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:5]
    d = WORK / jid
    (d / "out").mkdir(parents=True, exist_ok=True)
    meta = {"job": jid, "name": safe_stem(original_name), "created": time.time(),
            "kind": kind or kind_of(original_name)}
    if book_key:
        meta["book_key"] = book_key
        meta["book_key_version"] = BOOK_KEY_VERSION
    if source_hash:
        meta["source_hash"] = source_hash
    _write_meta(d, meta)
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
    meta["source_hash"] = hashlib.sha256(data).hexdigest()
    _write_meta(d, meta)
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
    """阅读版在哪个文件：AI 标定重渲过是 index.html，纯规则出的是 book.html。"""
    out = out_of(d)
    for name in ("index.html", "book.html"):
        f = out / name
        if f.is_file():
            return f
    return None


# ---------- 三段真实工作 ----------

def doctor(d: Path, src: Path, job: str | None = None) -> tuple[bool, str, Path]:
    """体检 + 前几章抽查页（引擎强制的确认步骤，整本 build 之前必须先过这一关）。"""
    ok, log = _run([str(RUN_PY), "doctor", str(src), "--chapters", "3", "--from", "1", "-o", str(d)],
                   900, job=job)
    return ok, log, d / "confirm" / "book.html"


def build(d: Path, src: Path, mode: str = "auto", job: str | None = None) -> tuple[bool, str]:
    """正式出阅读版：解析 -> 判定 -> 对照表 -> 渲染。"""
    return _run([str(RUN_PY), "build", str(src), "--mode", mode, "-o", str(out_of(d))],
                3600, job=job)


def render(d: Path, job: str | None = None) -> tuple[bool, str]:
    """AI 层改过 analyzed.json 之后，用同一份数据重渲一次 HTML。"""
    out = out_of(d)
    # render.py 在分章模式下把 -o 的参数当目录。build 也是传 out/；如果这里
    # 传 out/index.html，会生成 out/index/，而 reader_of 仍会打开旧的 index.html。
    # 单文件模式则必须传具体的 book.html 文件。
    if (out / "index.html").is_file():
        target = out
    elif (out / "book.html").is_file():
        target = out / "book.html"
    else:
        target = out
    ok, log = _run([str(RENDER_PY), str(out / "analyzed.json"), "-o", str(target),
                    "--mode", "auto"], 1800, job=job)
    if ok:
        _inject_keep_bridge(d, target)
    return ok, log


def _keep_values(doc: dict, page: str) -> list[bool]:
    """Return the keep flags for one generated chapter page."""
    page = Path(page).name
    if page == "book.html":
        return [bool(p.get("keep")) for ch in doc.get("chapters", [])
                for p in ch.get("paragraphs", []) if p.get("kind") != "separator"]
    match = re.fullmatch(r"ch(\d+)\.html", page)
    if not match:
        return []
    wanted = int(match.group(1))
    for ci, ch in enumerate(doc.get("chapters", [])):
        if int(ch.get("index", ci + 1)) == wanted:
            return [bool(p.get("keep")) for p in ch.get("paragraphs", [])
                    if p.get("kind") != "separator"]
    return []


def _inject_keep_bridge(d: Path, target: Path) -> None:
    """Add a small app-layer persistence bridge to generated reader pages.

    The engine template stays untouched. Paragraph order is the same as the
    analyzed chapter order, so the bridge only needs chapter/page + index.
    """
    try:
        doc = json.loads((out_of(d) / "analyzed.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    pages = [target] if target.is_file() else sorted(target.glob("ch*.html"))
    for page in pages:
        values = _keep_values(doc, page.name)
        if not values:
            continue
        try:
            html = page.read_text(encoding="utf-8")
        except OSError:
            continue
        script = (
            '<script data-qs-keep-bridge="1">'
            'const __qsJob=' + json.dumps(d.name) + ';'
            'const __qsPage=' + json.dumps(page.name) + ';'
            'const __qsInitialKeeps=' + json.dumps(values) + ';'
            '(function(){'
            'const ps=Array.from(document.querySelectorAll(".p"));'
            'ps.forEach(function(p,i){if(__qsInitialKeeps[i])p.classList.add("keep");'
            'p.addEventListener("click",function(){'
            'const keep=p.classList.contains("keep");'
            'fetch("/api/keep",{method:"POST",headers:{"content-type":"application/json"},'
            'body:JSON.stringify({job:__qsJob,page:__qsPage,index:i,keep:keep})})'
            '.catch(function(){try{localStorage.setItem("qs_keep:"+__qsJob+":"+__qsPage+":"+i,keep?"1":"0")}catch(e){}});'
            '});});'
            '})();'
            '</script>'
        )
        marker = "data-qs-keep-bridge=\"1\""
        if marker in html:
            html = re.sub(r'<script data-qs-keep-bridge="1">[\s\S]*?</script>', script, html)
        else:
            html = html.replace("</body>", script + "</body>")
        try:
            page.write_text(html, encoding="utf-8")
        except OSError:
            pass


def set_keep(d: Path, page: str, index: int, keep: bool) -> str:
    """Persist a reader keep decision in analyzed.json and keeps.json."""
    out = out_of(d)
    analyzed = out / "analyzed.json"
    doc = json.loads(analyzed.read_text(encoding="utf-8"))
    page = Path(page).name
    if page == "book.html":
        paras = [p for ch in doc.get("chapters", []) for p in ch.get("paragraphs", [])
                 if p.get("kind") != "separator"]
        para = paras[index] if 0 <= index < len(paras) else None
    else:
        match = re.fullmatch(r"ch(\d+)\.html", page)
        if not match:
            raise ValueError("不认识的阅读页")
        wanted = int(match.group(1))
        chapter = next((ch for ci, ch in enumerate(doc.get("chapters", []))
                        if int(ch.get("index", ci + 1)) == wanted), None)
        paras = ([p for p in chapter.get("paragraphs", [])
                  if p.get("kind") != "separator"] if chapter else [])
        para = paras[index] if 0 <= index < len(paras) else None
    if para is None:
        raise ValueError("段落不存在")
    para["keep"] = bool(keep)
    para["keep_updated"] = time.time()
    analyzed.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    sidecar = d / "keeps.json"
    try:
        keeps = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        keeps = {}
    keeps[str(para.get("pid") or f"{page}:{index}")] = bool(keep)
    sidecar.write_text(json.dumps(keeps, ensure_ascii=False), encoding="utf-8")
    return str(para.get("pid") or "")


def export(d: Path, preset: str = "balance", lever: str = "color",
           job: str | None = None) -> tuple[bool, str, Path]:
    """只出 EPUB。PDF 那一层要靠本机浏览器无头打印，大书会把进程拖死，已经去掉。"""
    out = out_of(d)
    target = out / f"{read_meta(d)['name']}.epub"
    args = [str(RUN_PY), "export", str(out / "analyzed.json"), "--format", "epub",
            "--preset", preset, "--lever", lever, "-o", str(target)]
    ok, log = _run(args, 1800, job=job)
    return ok, log, target


def _apply_keep_sidecar(d: Path) -> None:
    """Reapply durable reader keeps after a fresh build/review."""
    sidecar = d / "keeps.json"
    analyzed = out_of(d) / "analyzed.json"
    try:
        keeps = json.loads(sidecar.read_text(encoding="utf-8"))
        doc = json.loads(analyzed.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    changed = False
    for chapter in doc.get("chapters", []):
        for para in chapter.get("paragraphs", []):
            pid = str(para.get("pid") or "")
            if pid in keeps and bool(para.get("keep")) != bool(keeps[pid]):
                para["keep"] = bool(keeps[pid])
                changed = True
    if changed:
        analyzed.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def review(d: Path, endpoint: str, model: str, key: str,
           budget: int = 800, seed: int = 0, dry: bool = False,
           progress: Path | None = None,
           job: str | None = None) -> tuple[bool, str, dict]:
    """标定 + 定向升级，结果写回 analyzed.json。

    ``budget`` 是本轮 AI 总预算（抽样 + 升级），旧的 lo/hi 不再参与主流程。
    progress 给一个文件路径，AI 层会边跑边把进度写进去，服务端就能读出实时进度。
    第三个返回值是这一层的统计；最后一行 JSON 就是它。单批失败会记在
    failed 里，调用方必须看 failed 才能决定是否出书。
    """
    _apply_keep_sidecar(d)
    args = [str(AI_PY), str(out_of(d) / "analyzed.json"),
            "--endpoint", endpoint or "https://api.openai.com/v1",
            "--model", model or "gpt-4o-mini",
            "--max", str(max(0, int(budget))), "--seed", str(int(seed))]
    if dry:
        args.append("--dry")
    env = {"WNF_AI_KEY": key or ""}
    if progress is not None:
        env["WNF_PROGRESS"] = str(progress)
    ok, log = _run(args, 3600, env=env, job=job)

    stats: dict = {}
    for line in reversed((log or "").strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and "asked" in data:
            stats = data
            break
    return ok, log, stats


def ai_review(d: Path, endpoint: str, model: str, key: str, lo: float, hi: float,
              cap: int, dry: bool = False, progress: Path | None = None,
              job: str | None = None) -> tuple[bool, str, dict]:
    """兼容旧调用：lo/hi 忽略，cap 解释为总预算。"""
    return review(d, endpoint, model, key, budget=cap, seed=0, dry=dry,
                  progress=progress, job=job)


def read_progress(d: Path) -> dict:
    """AI 标定的实时进度（ai_layer 边跑边写 progress.json）。没跑过就是空。"""
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
    """AI 标定层实际使用的五分类提示词。给页面透明展示用。"""
    ok, log = _run([str(AI_PY), "--show-rules"], 60)
    return log if ok else ""


def read_meta(d: Path) -> dict:
    try:
        return json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        return {"job": d.name, "name": d.name}


def book_key_for_job(d: Path) -> str:
    """旧书没有 book_key 时，按源文件补算一次并写回 meta.json。

    只读接口也会走这里：第一次打开书架时把历史记录补齐，之后就不用再解析整本。
    """
    meta = read_meta(d)
    key = str(meta.get("book_key") or "").strip()
    try:
        version = int(meta.get("book_key_version") or 1)
    except (TypeError, ValueError):
        version = 1
    if key and version >= BOOK_KEY_VERSION:
        return key
    try:
        src = source_of(d)
        raw = src.read_bytes()
        from bookread import read_book  # noqa: PLC0415
        book = read_book(raw, src.name)
        key = book_key_of(book, str(meta.get("name") or src.name))
        meta["book_key"] = key
        meta["book_key_version"] = BOOK_KEY_VERSION
        meta.setdefault("source_hash", hashlib.sha256(raw).hexdigest())
        _write_meta(d, meta)
        return key
    except Exception:  # noqa: BLE001
        return ""


def find_job(*, book_key: str = "", source_hash: str = "") -> Path | None:
    """找已经入过架的同一本书：内容哈希或跨格式书身份命中都算。"""
    matches: list[Path] = []
    for d in WORK.glob("*"):
        if not d.is_dir() or not (d / "meta.json").is_file():
            continue
        meta = read_meta(d)
        digest = str(meta.get("source_hash") or "")
        key = str(meta.get("book_key") or "")
        if source_hash and not digest:
            try:
                src = source_of(d)
                digest = hashlib.sha256(src.read_bytes()).hexdigest()
            except Exception:  # noqa: BLE001
                digest = ""
        exact = bool(source_hash and digest == source_hash)
        same_book = False
        if book_key:
            try:
                version = int(meta.get("book_key_version") or 1)
            except (TypeError, ValueError):
                version = 1
            if not key or version < BOOK_KEY_VERSION:
                key = book_key_for_job(d)
            if key == book_key:
                same_book = True
        if exact or same_book:
            matches.append(d)
    if not matches:
        return None
    return choose_canonical_job(matches)


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


def library_of(d: Path) -> dict:
    """把一本书算成书架条目。状态从产物派生，不另存一份，免得两处对不上：
    只有源文件 = source（未判定）；有阅读版 = judged（已判定）；out/ 里有 .epub = exported。"""
    meta = read_meta(d)
    reader = reader_of(d)
    out = out_of(d)
    files = [{"name": f.name, "url": f"/work/{d.name}/out/{f.name}",
              "kb": round(f.stat().st_size / 1024)}
             for f in sorted(out.glob("*.epub")) if f.is_file()]
    state = "source"
    if reader is not None:
        state = "exported" if files else "judged"
    return {
        "id": d.name,
        "name": meta.get("name") or d.name,
        "kind": meta.get("kind") or "txt",
        "added": meta.get("created") or d.stat().st_mtime,
        "state": state,
        "progress": meta.get("progress") or {},
        "reader": f"/work/{d.name}/out/{reader.name}" if reader else "",
        "files": files,
    }


def best_progress(ds: list[Path]) -> dict:
    """同一本书的几份历史记录里，取读得最远的那个位置。"""
    best: dict = {}
    for d in ds:
        p = read_meta(d).get("progress") or {}
        if not isinstance(p, dict):
            continue
        try:
            chapter = int(p.get("chapter", -1))
            ratio = min(1.0, max(0.0, float(p.get("ratio", 0.0))))
        except (TypeError, ValueError):
            continue
        if chapter < 0:
            continue
        if not best or (chapter, ratio) > (int(best["chapter"]), float(best["ratio"])):
            best = {"chapter": chapter, "ratio": ratio}
    return best


def state_of(d: Path) -> str:
    reader = reader_of(d)
    if reader is None:
        return "source"
    return "exported" if any(out_of(d).glob("*.epub")) else "judged"


def choose_canonical_job(ds: list[Path]) -> Path:
    """一份书架条目只留一个 job 做门面，并把选择固定下来。

    优先已导出、已判定，再看续读位置和创建时间。选定后写 canonical_book，
    这样读者把进度往回调时，门面不会又切到另一份旧副本上。
    """
    flagged = [d for d in ds if read_meta(d).get("canonical_book")]
    if flagged:
        return max(flagged, key=lambda d: float(read_meta(d).get("created") or 0))

    ranks = {"source": 1, "judged": 2, "exported": 3}

    def rank(d: Path) -> tuple[int, int, float, float]:
        meta = read_meta(d)
        p = meta.get("progress") or {}
        try:
            chapter = int(p.get("chapter", -1))
            ratio = float(p.get("ratio", 0.0))
        except (TypeError, ValueError):
            chapter, ratio = -1, 0.0
        return (ranks.get(state_of(d), 0), chapter, ratio,
                float(meta.get("created") or 0.0))

    best = max(ds, key=rank)
    meta = read_meta(best)
    meta["canonical_book"] = True
    try:
        _write_meta(best, meta)
    except OSError:
        pass
    return best


def set_progress(d: Path, chapter: int, ratio: float) -> dict:
    """书架续读要的那点位置：第几章、章内比例。写进 meta.json，跟书一起留着。"""
    meta = read_meta(d)
    prog = {"chapter": max(0, int(chapter)), "ratio": min(1.0, max(0.0, float(ratio)))}
    meta["progress"] = prog
    _write_meta(d, meta)
    return prog


def rename_job(d: Path, name: str) -> str:
    """只改显示名，不动文件名与目录——产物路径全靠目录名，改了会全断。"""
    meta = read_meta(d)
    meta["name"] = str(name).strip()[:80] or meta.get("name") or d.name
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta["name"]
