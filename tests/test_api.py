"""深浅 · HTTP 接口契约测试。

跑法（在项目根目录）：
    python -m unittest discover -s tests -v                  # 快套（秒级）
    $env:WNF_SLOW="1"; python -m unittest discover -s tests -v   # 连完整链路

为什么单独测接口：网页端之后要接 Android，两边共用同一套 /api/*，
字段名一旦悄悄改名，另一个端就会莫名崩。这里把字段、状态码、错误信息钉住。

服务器起在系统随机端口（127.0.0.1:0），不碰你平时在用的 8765。
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import engine as E  # noqa: E402
import server as S  # noqa: E402
from helpers import WorkCase, make_epub, sample_txt  # noqa: E402


def _stub_config() -> dict:
    """一份齐全的 ai 配置，给「走不到标定那一步」的用例用。

    书没有源文件的话，判定会先失败，根本到不了 AI 标定，所以地址给个没人听的端口就行；
    真要跑标定的用例用 AiStub 的端口。
    """
    return {"endpoint": "http://127.0.0.1:9/v1", "model": "stub-model", "key": "sk-test"}


class AiStub:
    """假的 OpenAI 兼容接口：回一个符合五分类约定的数组。

    AI 标定现在是必经的一层，出书就得真的发一次请求。测试不该依赖外部模型，
    所以起一个本机小服务顶上，端点指向它——走的仍是 ai_layer 那条真路子。
    """

    def __init__(self) -> None:
        state = {"n": 0}
        labels = ("zero", "filler", "ornament", "neutral", "plot")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802  （父类就是这么命名的）
                n = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                # 新协议：五分类 + 原文引用。循环标签避免测试触发单标签退化阈值。
                asked = json.loads((body.get("messages") or [{}])[-1].get("content") or "[]")
                scored = []
                for it in asked:
                    label = labels[state["n"] % len(labels)]
                    state["n"] += 1
                    quote = (it.get("text") or it.get("prev") or it.get("next") or "")[:24]
                    scored.append({
                        "i": it["i"],
                        "label": label,
                        "quote": quote,
                        "why": "测试",
                        "rule_error": "none",
                        "rule_note": "none",
                    })
                payload = json.dumps(
                    {"choices": [{"message": {"content": json.dumps(scored, ensure_ascii=False)}}]},
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args) -> None:
                pass                                  # 测试里别刷屏

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def config(self) -> dict:
        """一份齐全的 ai 配置（/api/build 要的三样都在）。"""
        return {"endpoint": f"http://127.0.0.1:{self.httpd.server_address[1]}/v1",
                "model": "stub-model", "key": "sk-test"}

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class ApiCase(WorkCase):
    """给每个测试类起一个真实的 HTTP 服务，端口交给系统分配。"""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.httpd = S.ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        super().tearDownClass()

    # ---- 发请求的小工具 ----

    def http(self, method: str, path: str, body: bytes | None = None,
             headers: dict | None = None) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def http_json(self, method: str, path: str, obj=None,
                  headers: dict | None = None) -> tuple[int, dict]:
        body = None if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        h = {"content-type": "application/json"}
        h.update(headers or {})
        code, data = self.http(method, path, body, h)
        try:
            return code, json.loads(data.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return code, {}

    def post_file(self, path: str, raw: bytes, name: str) -> tuple[int, dict]:
        # 真实前端用 encodeURIComponent(file.name) 发这行 header
        code, data = self.http("POST", path, raw, {"x-filename": _q(name)})
        try:
            return code, json.loads(data.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return code, {}

    def upload(self, raw: bytes, name: str) -> tuple[int, dict]:
        return self.post_file("/api/upload", raw, name)

    def state(self, job: str) -> dict:
        return self.http_json("GET", f"/api/state?job={job}")[1]

    def wait_until(self, job: str, pred, what: str, timeout: int = 600) -> dict:
        """后台任务没有回调，只能轮询。"""
        deadline = time.time() + timeout
        st: dict = {}
        while time.time() < deadline:
            st = self.state(job)
            if pred(st):
                return st
            time.sleep(0.4)
        raise AssertionError(f"等 {what} 超时了，最后状态：{st.get('phase')}")


def _q(s: str) -> str:
    """把中文文件名编成 ASCII 的 header 值——http.client 只收 latin-1。"""
    from urllib.parse import quote
    return quote(s)


# ---------- 路由 ----------

class TestRouting(ApiCase):
    def test_health(self):
        code, data = self.http_json("GET", "/api/health")
        self.assertEqual(code, 200)
        self.assertIs(data["ok"], True)
        self.assertIs(data["readonly"], True)

    def test_three_pages(self):
        for path in ("/", "/app", "/read", "/shelf"):
            code, _ = self.http("GET", path)
            self.assertEqual(code, 200, path)

    def test_shelf_opens_books_in_reader(self):
        """书架点书统一进 /read，不再另开引擎生成的静态阅读页。"""
        code, body = self.http("GET", "/shelf")
        text = body.decode("utf-8", "replace")
        self.assertEqual(code, 200)
        self.assertIn("return '/read?' + q.join('&')", text)
        self.assertNotIn("window.open", text)

    def test_ui_has_budget_and_calibration_card(self):
        code, body = self.http("GET", "/app")
        text = body.decode("utf-8", "replace")
        self.assertEqual(code, 200)
        self.assertIn('id="budget"', text)
        self.assertIn('id="calCard"', text)
        self.assertIn("loadCalibration", text)
        self.assertIn("AI 个别裁定", text)
        self.assertIn("桶级标定", text)
        self.assertNotIn("lo: 0.30", text)

    def test_unknown_route_404(self):
        for path in ("/nope", "/api/nope"):
            code, data = self.http_json("GET", path)
            self.assertEqual(code, 404, path)
            self.assertIn("error", data)

    def test_serves_file_inside_job(self):
        d = self.make_job("书")
        (d / "confirm").mkdir()
        (d / "confirm" / "book.html").write_text("<i>x</i>", encoding="utf-8")
        code, body = self.http("GET", f"/work/{d.name}/confirm/book.html")
        self.assertEqual(code, 200)
        self.assertEqual(body, b"<i>x</i>")

    def test_chinese_download_filename_is_rfc5987_safe(self):
        d = self.make_job("书")
        out = d / "out"
        out.mkdir(exist_ok=True)
        target = out / "圣墟.epub"
        target.write_bytes(b"zip-bytes")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", f"/work/{d.name}/out/{_q(target.name)}?dl=1")
            resp = conn.getresponse()
            body = resp.read()
            self.assertEqual(resp.status, 200)
            self.assertEqual(body, b"zip-bytes")
            disposition = resp.getheader("content-disposition", "")
            self.assertIn("filename*=UTF-8''", disposition)
            self.assertIn("%E5%9C%A3%E5%A2%9F.epub", disposition)
        finally:
            conn.close()

    def test_blocks_traversal(self):
        """两种越界写法都不能把项目里的文件读出去。"""
        d = self.make_job("书")
        for path in (f"/work/{d.name}/../../app/server.py",
                     "/work/..%2fapp%2fserver.py",
                     "/work/../app/server.py"):
            code, _ = self.http("GET", path)
            self.assertIn(code, (403, 404), path)

    def test_unknown_extension_404(self):
        """白名单外的后缀不往外发。"""
        d = self.make_job("书")
        (d / "secret.py").write_text("x", encoding="utf-8")
        code, _ = self.http("GET", f"/work/{d.name}/secret.py")
        self.assertEqual(code, 404)


# ---------- /api/state ----------

class TestStateContract(ApiCase):
    """页面和 Android 都靠这个接口拿进度，字段名是契约。"""

    def test_missing_job_404(self):
        code, data = self.http_json("GET", "/api/state?job=20200101-000000-zzzzz")
        self.assertEqual(code, 404)
        self.assertIn("error", data)

    def test_field_names_are_stable(self):
        d = self.make_job("书")
        code, data = self.http_json("GET", f"/api/state?job={d.name}")
        self.assertEqual(code, 200)
        self.assertEqual(sorted(data),
                         ["done", "error", "job", "log", "phase", "progress", "result"])

    def test_progress_exposes_m1_fields(self):
        d = self.make_job("书")
        (d / "progress.json").write_text(json.dumps({
            "stage": "escalate", "sampled": 12, "fitted": 3,
            "escalated": 5, "total": 8, "done": 2,
            "reviewed": 17, "failed": 0, "pending": 4,
        }), encoding="utf-8")
        code, data = self.http_json("GET", f"/api/state?job={d.name}")
        self.assertEqual(code, 200)
        self.assertEqual(data["progress"]["stage"], "escalate")
        self.assertEqual(data["progress"]["sampled"], 12)
        self.assertEqual(data["progress"]["escalated"], 5)

    def test_keep_route_persists_pid(self):
        d = self.make_job("书")
        (d / "out").mkdir(exist_ok=True)
        doc = {"chapters": [{"index": 1, "title": "第一章", "paragraphs": [
            {"pid": "c1p1", "kind": "body", "text": "正文",
             "score": 0.55, "keep": False},
        ]}]}
        (d / "out" / "analyzed.json").write_text(json.dumps(doc), encoding="utf-8")
        with mock.patch.object(S.E, "render", return_value=(True, "ok")):
            code, data = self.http_json("POST", "/api/keep", {
                "job": d.name, "page": "ch0001.html", "index": 0, "keep": True,
            })
        self.assertEqual(code, 200, data)
        self.assertTrue(data["keep"])
        saved = json.loads((d / "out" / "analyzed.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["chapters"][0]["paragraphs"][0]["keep"])

    def test_calibration_route(self):
        d = self.make_job("书")
        code, _ = self.http_json("GET", f"/api/calibration?job={d.name}")
        self.assertEqual(code, 404)
        payload = {"version": 1, "n_sample": 12, "buckets": [],
                   "metrics": {"sample_coverage": 0.5}}
        (d / "calibration.json").write_text(json.dumps(payload), encoding="utf-8")
        code, data = self.http_json("GET", f"/api/calibration?job={d.name}")
        self.assertEqual(code, 200)
        self.assertEqual(data["n_sample"], 12)

    def test_infers_phase_from_disk(self):
        """服务重启后内存状态没了，要能从磁盘产物推出走到哪了。"""
        d = self.make_job("书")
        self.assertEqual(self.state(d.name)["phase"], "idle")

        (d / "confirm").mkdir()
        (d / "confirm" / "book.html").write_text("x", encoding="utf-8")
        st = self.state(d.name)
        self.assertEqual(st["phase"], "checked")
        self.assertIs(st["done"], False)

        (E.out_of(d) / "book.html").write_text("x", encoding="utf-8")
        st = self.state(d.name)
        self.assertEqual(st["phase"], "ready")
        self.assertIs(st["done"], True)


# ---------- /api/jobs ----------

class TestJobsContract(ApiCase):
    def test_list_shape_and_order(self):
        self.make_job("老书")
        b = self.make_job("新书")
        meta = json.loads((b / "meta.json").read_text(encoding="utf-8"))
        meta["created"] += 100
        (b / "meta.json").write_text(json.dumps(meta, ensure_ascii=False),
                                     encoding="utf-8")

        code, data = self.http_json("GET", "/api/jobs")
        self.assertEqual(code, 200)
        self.assertEqual([it["name"] for it in data["jobs"]], ["新书", "老书"])
        self.assertEqual(sorted(data["jobs"][0]),
                         ["created", "job", "name", "phase"])


# ---------- 上传校验 ----------

class TestUploadValidation(ApiCase):
    def test_empty_body_400(self):
        code, _ = self.http("POST", "/api/upload", b"", {"x-filename": "a.txt"})
        self.assertEqual(code, 400)

    def test_upload_reuses_same_book(self):
        """工作台上传同一本书两次，也复用同一个 job。"""
        raw = sample_txt(2)

        def fake_doctor(d, src, job=None):
            confirm = d / "confirm" / "book.html"
            confirm.parent.mkdir(exist_ok=True)
            confirm.write_text("x", encoding="utf-8")
            return True, "ok", confirm

        with mock.patch.object(E, "doctor", side_effect=fake_doctor):
            code, first = self.upload(raw, "同一本.txt")
            self.assertEqual(code, 200, first)
            code, second = self.upload(raw, "同一本.txt")
            self.assertEqual(code, 200, second)
        self.assertEqual(first["job"], second["job"])
        self.assertIs(second["reused"], True)
        code, library = self.http_json("GET", "/api/library")
        self.assertEqual(code, 200)
        self.assertEqual(len(library["books"]), 1)

    def test_pdf_rejected(self):
        code, data = self.post_file("/api/upload", b"%PDF-1.4", "a.pdf")
        self.assertEqual(code, 400)
        self.assertIn("PDF", data["error"])

    def test_broken_epub_400(self):
        code, data = self.post_file("/api/upload", b"not a zip at all", "坏.epub")
        self.assertEqual(code, 400)
        self.assertIn("error", data)


# ---------- 只读解析 ----------

class TestReadContract(ApiCase):
    """阅读器的预览接口：不落盘，纯解析，Android 端也会直接用它。"""

    def test_read_txt(self):
        code, data = self.post_file("/api/read", sample_txt(3), "书.txt")
        self.assertEqual(code, 200)
        self.assertIs(data["ok"], True)
        self.assertEqual(data["kind"], "txt")
        self.assertEqual(data["chapters"][0]["title"], "第1章 夜里的消息")

    def test_read_epub(self):
        raw = make_epub([("第一章 起", ["甲", "乙"])])
        code, data = self.post_file("/api/read", raw, "书.epub")
        self.assertEqual(code, 200)
        self.assertEqual(data["kind"], "epub")
        self.assertEqual(data["title"], "测试书")

    def test_empty_text_400(self):
        code, data = self.post_file("/api/read", "\n \t\n".encode("utf-8"), "空.txt")
        self.assertEqual(code, 400)
        self.assertIn("error", data)

    def test_read_book_uses_judged_tiers(self):
        """书架点开判定过的书：正文来自 analyzed.json，深浅档位要一起回来。"""
        d = self.make_job("判定过的书")
        E.save_source(d, "书.txt", sample_txt(2))
        doc = {"chapters": [{"index": 1, "title": "第一章", "paragraphs": [
            {"pid": "c1p0", "kind": "body", "text": "原文段落", "score": 0.9},
            {"pid": "c1p1", "kind": "separator", "text": "***", "score": 0.0},
            {"pid": "c1p2", "kind": "author_note", "text": "求票", "score": 0.5},
        ]}]}
        (E.out_of(d) / "analyzed.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")

        code, data = self.http_json("GET", f"/api/read-book?job={d.name}")
        self.assertEqual(code, 200, data)
        self.assertEqual(data["view"], "judged")
        self.assertEqual(data["name"], "判定过的书")
        self.assertEqual(data["chapters"][0]["paras"], ["原文段落", "求票"])
        self.assertEqual(data["chapters"][0]["tiers"], ["plot", "zero"])

    def test_read_book_falls_back_to_source(self):
        """还没判定的书照样能从书架打开，读的是原文。"""
        d = self.make_job("原文书")
        E.save_source(d, "书.txt", sample_txt(2))
        code, data = self.http_json("GET", f"/api/read-book?job={d.name}")
        self.assertEqual(code, 200, data)
        self.assertEqual(data["view"], "source")
        self.assertEqual(data["chapters"][0]["title"], "第1章 夜里的消息")

    def test_shelf_import_reuses_same_book(self):
        """同一份文件拖两次：第二次必须拿回同一个 job，书架不能多一条。"""
        raw = sample_txt(2)
        code, first = self.post_file("/api/read?shelf=1", raw, "同一本.txt")
        self.assertEqual(code, 200, first)
        code, second = self.post_file("/api/read?shelf=1", raw, "同一本.txt")
        self.assertEqual(code, 200, second)
        self.assertEqual(first["job"], second["job"])
        self.assertIs(second["reused"], True)
        code, library = self.http_json("GET", "/api/library")
        self.assertEqual(code, 200)
        self.assertEqual(len(library["books"]), 1)


# ---------- 删除 ----------

class TestJobDeleteContract(ApiCase):
    def test_deletes_dir_and_returns_list(self):
        d = self.make_job("要删的书")
        code, data = self.http_json("POST", "/api/job-delete", {"job": d.name})
        self.assertEqual(code, 200)
        self.assertIs(data["ok"], True)
        self.assertFalse(d.exists())
        self.assertNotIn(d.name, [it["job"] for it in data["jobs"]])

    def test_missing_job_404(self):
        code, data = self.http_json("POST", "/api/job-delete", {"job": "no-such-job"})
        self.assertEqual(code, 404)
        self.assertIn("error", data)

    def test_busy_job_409(self):
        """还在跑的任务不给删，防止删到一半目录被抽走。"""
        d = self.make_job("在跑的书")
        S.set_state(d.name, phase="build")
        code, data = self.http_json("POST", "/api/job-delete", {"job": d.name})
        self.assertEqual(code, 409)
        self.assertIn("error", data)
        self.assertTrue(d.exists())

    def test_deletes_duplicate_group(self):
        """历史重复记录是一本书的几个 job，删书要一起删，不能删完又冒出来。"""
        raw = sample_txt(2)
        _, d1 = E.new_job("同一本")
        E.save_source(d1, "同一本.txt", raw)
        _, d2 = E.new_job("同一本")
        E.save_source(d2, "同一本.txt", raw)
        code, data = self.http_json("POST", "/api/job-delete", {"job": d1.name})
        self.assertEqual(code, 200, data)
        self.assertFalse(d1.exists())
        self.assertFalse(d2.exists())
        self.assertEqual(sorted(data["deleted"]), sorted([d1.name, d2.name]))


# ---------- 取消 ----------

class TestCancelContract(ApiCase):
    """大书一次要跑一小时，Android 端也要能叫停，所以这条接口同样是契约。"""

    def test_missing_job_404(self):
        code, data = self.http_json("POST", "/api/cancel", {"job": "no-such-job"})
        self.assertEqual(code, 404)
        self.assertIn("error", data)

    def test_idle_job_409(self):
        d = self.make_job("闲着的书")
        code, data = self.http_json("POST", "/api/cancel", {"job": d.name})
        self.assertEqual(code, 409)
        self.assertIn("error", data)

    def test_cancels_running_job(self):
        d = self.make_job("在跑的书")
        S.set_state(d.name, phase="build")
        code, data = self.http_json("POST", "/api/cancel", {"job": d.name})
        self.assertEqual(code, 200)
        self.assertEqual(data["phase"], "cancelled")

        st = self.state(d.name)
        self.assertEqual(st["phase"], "cancelled")
        self.assertIs(st["done"], True)
        self.assertTrue(st["error"], "取消要留下说明，不能是空")

    def test_can_rerun_after_cancel(self):
        """取消过的任务还能重新跑，不会被「已经取消」卡住。"""
        d = self.make_job("要重跑的书")
        S.set_state(d.name, phase="render")
        self.assertEqual(self.http_json("POST", "/api/cancel", {"job": d.name})[0], 200)
        # 再点一次"出整本"：接口必须接住，说明"已取消"没把它挡在门外
        code, _ = self.http_json("POST", "/api/build", {"job": d.name, "ai": _stub_config()})
        self.assertEqual(code, 200)
        # 这本书没有源文件，后台线程会很快失败。等它收尾，别让它和临时目录的清理打架。
        self.wait_until(d.name, lambda s: s.get("phase") in ("ready", "error"), "重跑收尾", 60)

    def test_build_refuses_incomplete_ai_config(self):
        """AI 标定是必经的一层：接口 / 模型 / Key 缺一条就别想开工（旧的 on:false 也不算数）。"""
        d = self.make_job("没配接口的书")
        for ai in ({}, {"on": False}, {"endpoint": "https://x/v1", "model": "m", "key": ""},
                   {"endpoint": "", "model": "m", "key": "sk-x"}):
            code, data = self.http_json("POST", "/api/build", {"job": d.name, "ai": ai})
            self.assertEqual(code, 400, ai)
            self.assertIn("必须", data.get("error", ""))
        # 配齐了才收下
        code, _ = self.http_json("POST", "/api/build", {"job": d.name, "ai": _stub_config()})
        self.assertEqual(code, 200)
        self.wait_until(d.name, lambda s: s.get("phase") in ("ready", "error"), "收尾", 60)


# ---------- 状态持久化 ----------

class TestStatePersistence(ApiCase):
    """内存里的 STATE 一重启就没了。失败原因恰恰是最该留下的那个。"""

    def test_failure_reason_survives_restart(self):
        d = self.make_job("炸了的书")
        S.set_state(d.name, phase="error", done=True, error="引擎挂了：第 42 章解析失败")
        S.STATE.clear()                     # 模拟服务重启
        st = self.state(d.name)
        self.assertEqual(st["phase"], "error")
        self.assertEqual(st["error"], "引擎挂了：第 42 章解析失败")

    def test_interrupted_run_is_reported(self):
        d = self.make_job("跑到一半的书")
        S.set_state(d.name, phase="build")
        S.STATE.clear()
        st = self.state(d.name)
        # 重启那一刻它还在跑，那它其实已经被掐断了：不能装成"还在跑"
        self.assertNotIn(st["phase"], ("build", "ai", "render", "export"))
        self.assertIn("重启", st["error"])

    def test_log_survives_restart(self):
        d = self.make_job("有日志的书")
        S.log_line(d.name, "[error] 这里说清楚为什么炸")
        S.STATE.clear()
        self.assertIn("[error] 这里说清楚为什么炸", self.state(d.name)["log"])


# ---------- 完整链路（要跑引擎，默认跳过） ----------

@unittest.skipUnless(os.environ.get("WNF_SLOW") == "1",
                     "完整链路会真的跑引擎，设 WNF_SLOW=1 才跑")
class TestFullChain(ApiCase):
    """上传 → 出整本（含必经的 AI 标定）→ 打包 EPUB → 删掉。整条线上唯一的真活儿。"""

    def setUp(self) -> None:
        super().setUp()
        # AI 标定是必经的一层，出整本就会真的发请求；拿本机假接口顶上，别去碰外部模型。
        self.ai = AiStub()

    def tearDown(self) -> None:
        self.ai.close()
        # The cancelled build thread may release its Windows temp-directory
        # handle one tick after the process registry is empty.
        time.sleep(1.0)
        super().tearDown()

    def test_upload_build_export_delete(self):
        code, up = self.upload(sample_txt(10), "链路测试.txt")
        self.assertEqual(code, 200, up)
        job = up["job"]
        self.assertEqual(up["phase"], "checked")

        code, _ = self.http_json("POST", "/api/build", {"job": job, "ai": self.ai.config})
        self.assertEqual(code, 200, job)
        st = self.wait_until(job, lambda s: s.get("phase") in ("ready", "error"),
                             "整本出完")
        self.assertEqual(st["phase"], "ready", st.get("error"))
        self.assertTrue(st["result"]["reader"], "阅读版地址不能是空")

        code, _ = self.http_json("POST", "/api/export",
                                 {"job": job, "preset": "balance", "lever": "color"})
        self.assertEqual(code, 200, job)
        st = self.wait_until(job, lambda s: any(f["name"].endswith(".epub")
                                                for f in s["result"]["files"]),
                             "EPUB 打包好")
        self.assertEqual([f["name"] for f in st["result"]["files"]],
                         ["链路测试.epub"])

        url = st["result"]["files"][0]["url"]
        # 产物名是中文，浏览器会自动编码；直接发请求得自己编一下
        code, body = self.http("GET", _q(url))
        self.assertEqual(code, 200)
        self.assertEqual(body[:2], b"PK")

        code, data = self.http_json("POST", "/api/job-delete", {"job": job})
        self.assertEqual(code, 200)
        self.assertNotIn(job, [it["job"] for it in data["jobs"]])

    def test_cancel_stops_a_real_run(self):
        """真的跑着引擎的时候叫停：进程要真的退掉，阶段要停在已取消。"""
        code, up = self.upload(sample_txt(40), "叫停测试.txt")
        self.assertEqual(code, 200, up)
        job = up["job"]

        self.http_json("POST", "/api/build", {"job": job, "ai": self.ai.config})
        code, _ = self.http_json("POST", "/api/cancel", {"job": job})
        self.assertEqual(code, 200, "任务刚起跑就叫停，不该被拒")

        st = self.wait_until(job, lambda s: s.get("phase") in ("cancelled", "ready", "error"),
                             "任务停下来", 120)
        self.assertEqual(st["phase"], "cancelled", st.get("error"))

        # 等等子进程真的退干净，别让它和临时目录的清理打架
        deadline = time.time() + 60
        while E.PROCS and time.time() < deadline:
            time.sleep(0.2)
        self.assertFalse(E.PROCS, "取消之后引擎子进程没退干净")
        # Windows 上服务线程可能比子进程晚一拍释放 work/ 里的日志句柄。
        time.sleep(1.0)


if __name__ == "__main__":
    unittest.main()
