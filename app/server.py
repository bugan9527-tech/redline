"""深浅 · 本地后端：一个文件，只用 Python 标准库，不需要装任何东西。

    python app/server.py            # 打开 http://127.0.0.1:8765
    python app/server.py --port 9000 --no-open
    python app/server.py --host 0.0.0.0       # 给同一个局域网里的人用（默认只开本机）
    python app/server.py --log work/server.log   # 无控制台时（pythonw）也让日志有地方落

已经在跑的时候再启动一次不会报错：它先探一下 /api/health，发现是自己就把页面打开、安静退出。

它不碰 engine/webnovel-focus（那是只读原件）：每一次处理都是把引擎自己的
命令行入口当子进程调一次，产物全部落在项目内的 work/。

接口（首页 / ，工作台在 /app，阅读器在 /read）：
    GET  /                          首页
    GET  /app                       工作台
    GET  /read                      阅读器
    POST /api/upload                body = TXT / EPUB 字节；header x-filename；返回 job + 抽查页
    POST /api/build                 {job, ai:{endpoint,model,key,lo,hi,max}}  后台跑；三样缺一条回 400
    POST /api/export                {job, preset, lever}  打包 EPUB（后台跑）
    POST /api/job-delete            {job}  删掉这本书的整个工作目录
    POST /api/cancel                {job}  取消正在跑的任务（掐掉引擎子进程）
    POST /api/ai-test               {endpoint, model, key}  测一次连接能不能用
    POST /api/read                  body = TXT/EPUB 字节；header x-filename；返回分好章的正文
    GET  /api/state?job=            进度与产物
    GET  /api/skill-rules           AI 复核实际使用的判定标准（只读自 skill）
    GET  /work/<job>/...            抽查页 / 阅读版 / EPUB 下载
    GET  /assets/...                落地页图片
"""

from __future__ import annotations

import json
import mimetypes
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine as E  # noqa: E402
import bookread as B  # noqa: E402

ROOT = E.ROOT
WORK = E.WORK
UI = Path(__file__).resolve().parent / "ui.html"
READER = Path(__file__).resolve().parent / "reader.html"
MAX_UPLOAD = 80 * 1024 * 1024          # 一本书不该超过 80 MB
SERVE_EXT = {".html", ".txt", ".json", ".epub", ".jpg", ".jpeg", ".png", ".css", ".js"}
LOG_KEEP = 60                          # 页面上最多显示这么多行日志
# 这几个阶段的意思都是"正在跑"。服务重启的那一刻它们还在跑，说明已经被掐断了。
BUSY_PHASES = ("doctor", "build", "ai", "render", "export")

STATE: dict[str, dict] = {}
CANCEL: set[str] = set()               # 被用户取消过的任务，见 /api/cancel
LOCK = threading.Lock()


def blank_state(job: str) -> dict:
    return {"job": job, "phase": "idle", "log": [], "done": False, "error": None}


def state_file(job: str) -> Path:
    return WORK / job / "state.json"


def run_log(job: str) -> Path:
    return WORK / job / "run.log"


def save_state(job: str, st: dict) -> None:
    """状态落盘。内存里的 STATE 一重启就没了，失败原因得留下来。"""
    try:
        state_file(job).write_text(json.dumps(st, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    except OSError:
        pass


def tail_log(job: str) -> list[str]:
    try:
        text = run_log(job).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [l for l in text.splitlines() if l.strip()][-LOG_KEEP:]


def load_state(job: str, d: Path) -> dict:
    """内存里没有这台任务的实时状态（服务重启过），从 state.json + run.log 复原。"""
    try:
        saved = json.loads(state_file(job).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        saved = {}
    ph = saved.get("phase") or ""
    err = saved.get("error")
    if ph in BUSY_PHASES:
        # 重启那一刻它还在跑，那它其实已经被掐断了。说清楚，别装没事。
        ph = phase_on_disk(d)
        err = err or "服务重启过，这次任务被中断了，重跑一次。"
    if not ph:
        ph = phase_on_disk(d)
    return {"job": job, "phase": ph, "log": tail_log(job) or saved.get("log") or [],
            "done": ph == "ready", "error": err}


def set_state(job: str, **kw) -> None:
    with LOCK:
        st = STATE.setdefault(job, blank_state(job))
        st.update(kw)
        save_state(job, st)


def log_line(job: str, line: str) -> None:
    if not line:
        return
    lines = [l for l in line.splitlines() if l.strip()]
    if not lines:
        return
    with LOCK:
        st = STATE.setdefault(job, blank_state(job))
        st["log"] = (st["log"] + lines)[-LOG_KEEP:]
    try:
        with open(run_log(job), "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def job_dir(job: str) -> Path:
    d = (WORK / job).resolve()
    if WORK.resolve() not in d.parents or not d.is_dir():
        raise FileNotFoundError(job)
    return d


def phase_on_disk(d: Path) -> str:
    """内存里没有这台任务的实时状态（比如服务重启过），就按磁盘上的产物判断它走到哪了。"""
    if E.reader_of(d) is not None:
        return "ready"
    if (d / "confirm" / "book.html").is_file():
        return "checked"
    return "idle"


def phase_now(d: Path) -> str:
    """在跑的用内存里的实时阶段，否则按磁盘判断。"""
    with LOCK:
        st = STATE.get(d.name)
    return (st or {}).get("phase") or phase_on_disk(d)


def list_jobs(limit: int = 50) -> list[dict]:
    """work/ 里做过的书，新的在前。「以前的书」那块用。"""
    items = []
    for d in WORK.glob("*"):
        if not d.is_dir() or not (d / "meta.json").is_file():
            continue
        meta = E.read_meta(d)
        items.append({"job": d.name, "name": meta.get("name", d.name),
                      "created": meta.get("created") or d.stat().st_mtime,
                      "phase": phase_now(d)})
    items.sort(key=lambda it: it["created"] or 0, reverse=True)
    return items[:limit]


# ---------- 后台任务 ----------

def task_build(job: str, ai: dict) -> None:
    try:
        d = job_dir(job)
        src = E.source_of(d)
        try:
            (d / "progress.json").unlink()          # 上一次的进度别留着
        except OSError:
            pass

        set_state(job, phase="build", done=False, error=None, started=time.time())
        ok, log = E.build(d, src, job=job)
        if job in CANCEL:
            return                       # 被取消了：不记引擎那行"退出码 1"，会看着像出错
        log_line(job, log)
        if not ok:
            raise RuntimeError("生成阅读版失败，看日志里的最后几行。")

        # AI 复核是必经的一层，没有「不接」这条分支：接口没配好在 /api/build 那关就被拦下了。
        # 复核没跑成就出错，不静默退回纯规则——那样等于把这一层悄悄抽掉了。
        set_state(job, phase="ai", progress={})
        lo = float(ai.get("lo") or 0.3)
        hi = float(ai.get("hi") or 0.7)
        cap = int(ai.get("max") or 400)
        review_ok, review_log, stats = E.ai_review(
            d, ai.get("endpoint", ""), ai.get("model", ""), ai.get("key", ""), lo, hi, cap,
            progress=d / "progress.json", job=job)
        if job in CANCEL:
            return
        log_line(job, review_log)
        if not review_ok:
            raise RuntimeError("AI 复核这一层没跑成，这次不出书。看日志里的最后几行。")
        if stats.get("failed"):
            raise RuntimeError(
                f"AI 复核有 {stats['failed']} 批没跑成（网络或额度的问题），这次不出书；"
                "确认接口能用之后重新出整本。")
        # 复核改的是 analyzed.json，重渲一次让 HTML 跟上
        set_state(job, phase="render")
        render_ok, render_log = E.render(d, job=job)
        if job in CANCEL:
            return
        log_line(job, render_log)
        if not render_ok:
            raise RuntimeError("复核完了，但重渲失败，看日志里的最后几行。")

        if job in CANCEL:
            return
        set_state(job, phase="ready", done=True, error=None, result=E.result_of(d))
    except Exception as exc:  # noqa: BLE001
        if job in CANCEL:
            return                       # 别把取消盖成「出错」
        log_line(job, f"[error] {exc}")
        set_state(job, phase="error", done=True, error=str(exc))


def task_export(job: str, preset: str, lever: str) -> None:
    try:
        d = job_dir(job)
        set_state(job, phase="export", error=None)
        ok, log, target = E.export(d, preset=preset, lever=lever, job=job)
        if job in CANCEL:
            return
        log_line(job, log)
        if not ok or not target.exists():
            raise RuntimeError("EPUB 打包失败，看日志里的最后几行。")
        set_state(job, phase="ready", done=True, error=None, result=E.result_of(d))
    except Exception as exc:  # noqa: BLE001
        if job in CANCEL:
            return
        log_line(job, f"[error] {exc}")
        set_state(job, phase="error", done=True, error=str(exc))


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "qianshen/0.1"

    def log_message(self, fmt: str, *args) -> None:  # 静音，日志我们自己收
        pass

    # 小工具
    def send_file(self, path: Path, download: bool = False) -> None:
        if not path.is_file() or path.suffix.lower() not in SERVE_EXT:
            return self.fail(404, "找不到这个文件")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("content-type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        if download:
            self.send_header("content-disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code: int = 200, extra: dict | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, code: int, msg: str) -> None:
        self.send_json({"error": msg}, code)

    def read_json(self) -> dict:
        n = int(self.headers.get("content-length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def job_path(self, job: str) -> Path | None:
        """取出这次请求要动的任务目录；不存在就回 404，返回 None 让调用方收工。"""
        try:
            return job_dir(job)
        except FileNotFoundError:
            self.fail(404, "没有这个任务")
            return None

    # 路由
    def do_GET(self) -> None:  # noqa: N802
        try:
            self.route_get()
        except Exception as exc:  # noqa: BLE001
            self.fail(500, f"服务端出错：{exc}")

    def do_POST(self) -> None:  # noqa: N802
        try:
            self.route_post()
        except Exception as exc:  # noqa: BLE001
            self.fail(500, f"服务端出错：{exc}")

    def route_get(self) -> None:
        url = urlparse(self.path)
        q = parse_qs(url.query)
        path = unquote(url.path)

        if path in ("/", "/index.html"):
            return self.send_file(ROOT / "index.html")
        if path == "/api/health":
            # tiers 是"这版服务带不带分层解析"的标记。bookread 是启动时 import 进内存的，
            # 改了代码不重启，阅读器就读不出深浅——页面靠这个字段把话说清楚。
            return self.send_json({"ok": True, "engine": str(E.ENGINE), "readonly": True,
                                   "tiers": True})
        if path == "/api/skill-rules":
            text = E.skill_rules()
            return self.send_text(text or "（没读到判定标准，AI 复核会用内置的默认口径）")
        if path in ("/app", "/app/"):
            return self.send_file(UI)
        if path in ("/read", "/read/"):
            return self.send_file(READER)
        if path == "/api/state":
            job = (q.get("job") or [""])[0]
            d = self.job_path(job)
            if d is None:
                return
            with LOCK:
                st = STATE.get(job)
            # 内存里没有（服务重启过）就从磁盘复原，失败原因不会丢
            out = dict(st) if st else load_state(job, d)
            out["result"] = (st or {}).get("result") or E.result_of(d)
            out["progress"] = E.read_progress(d)
            return self.send_json(out)
        if path == "/api/jobs":
            return self.send_json({"jobs": list_jobs()})
        if path.startswith("/work/"):
            rel = path[len("/work/"):]
            d = self.job_path(rel.split("/")[0])
            if d is None:
                return
            target = (WORK / rel).resolve()
            if WORK.resolve() not in target.parents:
                return self.fail(403, "越界了")
            return self.send_file(target, download=(q.get("dl") or [""])[0] == "1")
        if path.startswith("/assets/"):
            target = (ROOT / path.lstrip("/")).resolve()
            if ROOT.resolve() not in target.parents:
                return self.fail(403, "越界了")
            return self.send_file(target)
        return self.fail(404, "没有这个地址")

    def route_post(self) -> None:
        path = urlparse(self.path).path

        if path == "/api/upload":
            n = int(self.headers.get("content-length") or 0)
            if n <= 0:
                return self.fail(400, "没有收到文件")
            if n > MAX_UPLOAD:
                return self.fail(413, "文件超过 80 MB")
            raw = self.rfile.read(n)
            name = unquote(self.headers.get("x-filename") or "book.txt")
            low = name.lower()
            if low.endswith(".txt"):
                data = raw
            elif low.endswith(".epub"):
                try:
                    # 引擎只吃 TXT，先把 EPUB 摊平成章节分明的文本
                    data = B.to_txt(B.read_book(raw, name)).encode("utf-8")
                except Exception as exc:  # noqa: BLE001
                    return self.fail(400, f"这个 EPUB 读不出来：{exc}")
            else:
                return self.fail(400, "只吃 TXT 和 EPUB。PDF 要先抽成文本，这层还没做。")
            job, d = E.new_job(name)
            src = E.save_source(d, name, data)
            set_state(job, phase="doctor", done=False, error=None)
            ok, log, confirm = E.doctor(d, src, job=job)
            log_line(job, log)
            if not ok or not confirm.exists():
                set_state(job, phase="error", done=True, error="体检 / 抽查失败，看日志。")
                return self.send_json({"job": job, "phase": "error", "log": [log[-1200:]]}, 500)
            set_state(job, phase="checked", done=False, result=E.result_of(d))
            return self.send_json({"job": job, "phase": "checked", "confirm": E.result_of(d)["confirm"]})

        if path == "/api/read":
            n = int(self.headers.get("content-length") or 0)
            if n <= 0:
                return self.fail(400, "没有收到文件")
            if n > MAX_UPLOAD:
                return self.fail(413, "文件超过 80 MB")
            raw = self.rfile.read(n)
            name = unquote(self.headers.get("x-filename") or "book.txt")
            try:
                book = B.read_book(raw, name)
            except Exception as exc:  # noqa: BLE001
                return self.fail(400, f"读不出来：{exc}")
            book["ok"] = True
            return self.send_json(book)

        if path == "/api/ai-test":
            body = self.read_json()
            if not (body.get("key") or "").strip():
                return self.send_json({"ok": False, "message": "先填 API Key 再测。"})
            ok, msg = E.ai_probe(body.get("endpoint", ""), body.get("model", ""),
                                 body.get("key", ""))
            return self.send_json({"ok": ok, "message": msg})

        if path == "/api/build":
            body = self.read_json()
            job = body.get("job") or ""
            if self.job_path(job) is None:
                return
            ai = body.get("ai") or {}
            # AI 复核是必须的一层，不是可选开关：三样缺一条就不给出书。
            if not (str(ai.get("endpoint") or "").strip()
                    and str(ai.get("model") or "").strip()
                    and str(ai.get("key") or "").strip()):
                return self.fail(400, "AI 复核是必须的一层：接口地址、模型名、Key 都填上才出书。")
            if STATE.get(job, {}).get("phase") in BUSY_PHASES:
                return self.fail(409, "已经在跑了")
            CANCEL.discard(job)              # 上一次取消过的话，这次重新算数
            set_state(job, phase="build", done=False, error=None)
            threading.Thread(target=task_build, args=(job, ai), daemon=True).start()
            return self.send_json({"job": job, "phase": "build", "started": True})

        if path == "/api/export":
            body = self.read_json()
            job = body.get("job") or ""
            if self.job_path(job) is None:
                return
            if STATE.get(job, {}).get("phase") in BUSY_PHASES:
                return self.fail(409, "已经在跑了")
            CANCEL.discard(job)
            set_state(job, phase="export", done=False, error=None)
            threading.Thread(target=task_export,
                             args=(job, body.get("preset") or "balance",
                                   body.get("lever") or "color"), daemon=True).start()
            return self.send_json({"job": job, "phase": "export", "started": True})

        if path == "/api/cancel":
            body = self.read_json()
            job = body.get("job") or ""
            if self.job_path(job) is None:
                return
            if STATE.get(job, {}).get("phase") not in BUSY_PHASES:
                return self.fail(409, "这本书没在跑，不用取消。")
            CANCEL.add(job)                  # 先立旗：跑任务的那条线程照它决定要不要写结局
            killed = E.cancel(job)           # 再把正在跑的子进程掐掉
            log_line(job, "[cancelled] 用户取消了这次任务。")
            set_state(job, phase="cancelled", done=True,
                      error="已取消，可以重新跑一次。")
            return self.send_json({"ok": True, "job": job, "phase": "cancelled",
                                   "killed": killed})

        if path == "/api/job-delete":
            body = self.read_json()
            job = body.get("job") or ""
            d = self.job_path(job)
            if d is None:
                return
            if STATE.get(job, {}).get("phase") in BUSY_PHASES:
                return self.fail(409, "这本书还在跑，等它停下来再删。")
            try:
                E.remove_job(d)
            except Exception as exc:  # noqa: BLE001
                return self.fail(500, f"删不掉：{exc}")
            with LOCK:
                STATE.pop(job, None)
            CANCEL.discard(job)
            return self.send_json({"ok": True, "job": job, "jobs": list_jobs()})

        return self.fail(404, "没有这个地址")


def already_running(shown: str, port: int) -> bool:
    """探一下 /api/health：这个端口上已经是我们自己在跑吗。"""
    try:
        with urllib.request.urlopen(f"http://{shown}:{port}/api/health", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def setup_log(path: str) -> None:
    """--log 给的文件：pythonw 没有控制台，把输出接进文件，出问题才有地方可查。"""
    f = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout = f
    sys.stderr = f
    print(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====")


def main() -> None:
    argv = sys.argv[1:]
    port = 8765
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    host = "127.0.0.1"                 # 默认只开给本机；要给别人用就 --host 0.0.0.0
    if "--host" in argv:
        host = argv[argv.index("--host") + 1]
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{shown}:{port}"

    WORK.mkdir(parents=True, exist_ok=True)
    if "--log" in argv:
        setup_log(argv[argv.index("--log") + 1])

    if already_running(shown, port):
        # 已经有一个在跑就别再起第二个（会端口冲突）。把页面打开就好。
        print(f"深浅已经在运行：{url}（这次不再启动一个）")
        if "--no-open" not in argv:
            webbrowser.open(url)
        raise SystemExit(0)

    if not E.ENGINE.is_dir():
        print(f"找不到引擎副本：{E.ENGINE}\n先把 webnovel-focus 复制到 engine/ 下。", file=sys.stderr)
        raise SystemExit(2)

    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print(f"端口 {port} 起不来：{exc}\n换一个端口试试：python app/server.py --port 9000")
        raise SystemExit(1)

    print(f"深浅 · 本地后端已启动  {url}   （首页 / ，工作台 {url}/app）")
    print(f"引擎（只读）：{E.ENGINE}")
    print(f"产物目录：{WORK}")
    if host not in ("127.0.0.1", "localhost"):
        print("对局域网开放了：同一个 Wi-Fi 下的人都能用，别开到公网去。")
    if "--no-open" not in argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停了。")


if __name__ == "__main__":
    main()
