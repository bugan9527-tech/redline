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
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import engine as E  # noqa: E402
import server as S  # noqa: E402
from helpers import WorkCase, make_epub, sample_txt  # noqa: E402


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
        for path in ("/", "/app", "/read"):
            code, _ = self.http("GET", path)
            self.assertEqual(code, 200, path)

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
        code, _ = self.http_json("POST", "/api/build", {"job": d.name, "ai": {"on": False}})
        self.assertEqual(code, 200)
        # 这本书没有源文件，后台线程会很快失败。等它收尾，别让它和临时目录的清理打架。
        self.wait_until(d.name, lambda s: s.get("phase") in ("ready", "error"), "重跑收尾", 60)


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
    """上传 → 出整本 → 打包 EPUB → 删掉。整条线上唯一的真活儿。"""

    def test_upload_build_export_delete(self):
        code, up = self.upload(sample_txt(10), "链路测试.txt")
        self.assertEqual(code, 200, up)
        job = up["job"]
        self.assertEqual(up["phase"], "checked")

        code, _ = self.http_json("POST", "/api/build", {"job": job, "ai": {"on": False}})
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

        self.http_json("POST", "/api/build", {"job": job, "ai": {"on": False}})
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


if __name__ == "__main__":
    unittest.main()
