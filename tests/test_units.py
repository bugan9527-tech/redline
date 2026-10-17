"""深浅 · 应用层单元测试（不跑引擎，秒级）。

跑法（在项目根目录）：
    python -m unittest discover -s tests -v

覆盖 app/engine.py、app/server.py、app/bookread.py 三个自有模块。
engine/webnovel-focus/tests/ 属上游引擎，不归这里管。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bookread as B  # noqa: E402
import engine as E  # noqa: E402
import server as S  # noqa: E402
from helpers import WorkCase, make_epub  # noqa: E402


# ---------- app/engine.py ----------

class TestSafeStem(unittest.TestCase):
    """书名要当文件名用：中文留着，路径符号 / 通配符一律压成下划线。"""

    def test_keeps_chinese(self):
        self.assertEqual(E.safe_stem("序列玩家.txt"), "序列玩家")

    def test_strips_extension(self):
        self.assertEqual(E.safe_stem("a.b.txt"), "a.b")

    def test_replaces_unsafe(self):
        self.assertEqual(E.safe_stem("a b*c?.txt"), "a_b_c_")

    def test_truncates(self):
        self.assertEqual(len(E.safe_stem("长" * 100 + ".txt")), 48)

    def test_falls_back_when_empty(self):
        self.assertEqual(E.safe_stem(""), "book")

    def test_unsafe_only_becomes_underscores(self):
        """全是符号的名字会被压成一个下划线，不空就行（连续的符号压成一段）。"""
        self.assertEqual(E.safe_stem("???"), "_")


class TestReaderOf(WorkCase):
    """阅读版有两个可能的文件名，选错就是 404。"""

    def test_none_before_render(self):
        self.assertIsNone(E.reader_of(self.make_job()))

    def test_book_html(self):
        d = self.make_job()
        (E.out_of(d) / "book.html").write_text("x", encoding="utf-8")
        self.assertEqual(E.reader_of(d).name, "book.html")

    def test_index_wins(self):
        """AI 标定会重渲成 index.html，两个都在时以它为准。"""
        d = self.make_job()
        (E.out_of(d) / "book.html").write_text("x", encoding="utf-8")
        (E.out_of(d) / "index.html").write_text("x", encoding="utf-8")
        self.assertEqual(E.reader_of(d).name, "index.html")


class TestResultOf(WorkCase):
    """result_of 是页面和 Android 端共用的数据源。"""

    def setUp(self):
        super().setUp()
        self.d = self.make_job("测试书")
        self.out = E.out_of(self.d)

    def test_never_lists_chapter_shards(self):
        """回归：引擎渲染的 chXXXX.html 分片有上千个，绝不能进下载列表。"""
        (self.out / "ch0001.html").write_text("x", encoding="utf-8")
        (self.out / "ch0002.html").write_text("x", encoding="utf-8")
        (self.out / "book.html").write_text("x", encoding="utf-8")
        (self.out / "测试书.epub").write_bytes(b"PK\x03\x04")
        self.assertEqual([f["name"] for f in E.result_of(self.d)["files"]],
                         ["测试书.epub"])

    def test_files_empty_before_export(self):
        (self.out / "book.html").write_text("x", encoding="utf-8")
        self.assertEqual(E.result_of(self.d)["files"], [])

    def test_reader_follows_actual_file(self):
        (self.out / "book.html").write_text("x", encoding="utf-8")
        r = E.result_of(self.d)
        self.assertTrue(r["reader"].endswith("/out/book.html"))
        self.assertTrue(r["confirm"].endswith("/confirm/book.html"))

    def test_reader_blank_before_render(self):
        self.assertEqual(E.result_of(self.d)["reader"], "")

    def test_keys_are_stable(self):
        """字段名就是契约：以后加字段可以，改名字是破坏性变更。"""
        self.assertEqual(sorted(E.result_of(self.d)),
                         ["confirm", "files", "job", "name", "prescan",
                          "reader", "report", "tiers"])


class TestSourceOf(WorkCase):
    """工作目录里还堆着引擎自己的产物，别把产物当成书。"""

    def test_skips_engine_artifacts(self):
        d = self.make_job("书")
        (d / "prescan.txt").write_text("x", encoding="utf-8")
        (d / "report.txt").write_text("x", encoding="utf-8")
        (d / "书.txt").write_text("正文", encoding="utf-8")
        self.assertEqual(E.source_of(d).name, "书.txt")

    def test_meta_src_wins(self):
        d = self.make_job("书")
        (d / "prescan.txt").write_text("x", encoding="utf-8")
        E.save_source(d, "书.txt", "正文".encode("utf-8"))
        self.assertEqual(E.source_of(d), d / "书.txt")

    def test_raises_when_missing(self):
        with self.assertRaises(FileNotFoundError):
            E.source_of(self.make_job("书"))


class TestProgress(WorkCase):
    """AI 标定的实时进度：没跑过要给空 dict，不能抛。"""

    def test_empty_when_absent(self):
        self.assertEqual(E.read_progress(self.make_job()), {})


class TestBookIdentity(WorkCase):
    """同一本书反复导入 / 换格式导入，都不能在书架上变成另一本。"""

    def _pair(self):
        txt = "第1章 起\n\n甲\n\n乙\n\n第2章 落\n\n丙\n\n丁\n".encode("utf-8")
        epub = make_epub([("第1章 起", ["甲", "乙"]),
                          ("第2章 落", ["丙", "丁"])], title="书")
        return txt, epub

    def test_txt_and_epub_share_identity(self):
        txt, epub = self._pair()
        self.assertEqual(
            E.book_key_of(B.read_book(txt, "书.txt"), "书.txt"),
            E.book_key_of(B.read_book(epub, "书.epub"), "书.epub"),
        )

    def test_identity_ignores_cover_page(self):
        txt, _ = self._pair()
        epub = make_epub([("封面", ["x"]),
                          ("第1章 起", ["甲", "乙"]),
                          ("第2章 落", ["丙", "丁"])], title="书")
        self.assertEqual(
            E.book_key_of(B.read_book(txt, "书.txt"), "书.txt"),
            E.book_key_of(B.read_book(epub, "书.epub"), "书.epub"),
        )

    def test_identity_ignores_title_metadata(self):
        """EPUB 元数据标题和 TXT 文件名不同，不应阻止同一正文合并。"""
        txt, _ = self._pair()
        epub = make_epub([("第1章 起", ["甲", "乙"]),
                          ("第2章 落", ["丙", "丁"])], title="另一个标题")
        self.assertEqual(
            E.book_key_of(B.read_book(txt, "不同名的文件.txt"), "不同名的文件.txt"),
            E.book_key_of(B.read_book(epub, "另一个标题.epub"), "另一个标题.epub"),
        )

    def test_different_body_same_title_is_not_merged(self):
        """标题相同但正文不同，仍然必须是两本书。"""
        txt, _ = self._pair()
        epub = make_epub([("第1章 起", ["完全不同的正文"]),
                          ("第2章 落", ["另一段正文"])], title="书")
        self.assertNotEqual(
            E.book_key_of(B.read_book(txt, "书.txt"), "书.txt"),
            E.book_key_of(B.read_book(epub, "书.epub"), "书.epub"),
        )

    def test_cross_format_import_reuses_job_with_different_titles(self):
        """改文件名、换 EPUB 标题后导入，仍应复用同一个书架 job。"""
        txt, _ = self._pair()
        epub = make_epub([("第1章 起", ["甲", "乙"]),
                          ("第2章 落", ["丙", "丁"])], title="另一个标题")
        job1, d1, _ = S.save_book_from_raw("不同名的文件.txt", txt)
        job2, d2, reused = S.save_book_from_raw("另一个标题.epub", epub)
        self.assertEqual(job1, job2)
        self.assertEqual(d1, d2)
        self.assertTrue(reused)

    def test_reimport_reuses_job(self):
        txt, _ = self._pair()
        job1, d1, reused1 = S.save_book_from_raw("书.txt", txt)
        job2, d2, reused2 = S.save_book_from_raw("书.txt", txt)
        self.assertEqual(job1, job2)
        self.assertEqual(d1, d2)
        self.assertFalse(reused1)
        self.assertTrue(reused2)
        self.assertEqual(len(list(self.work.iterdir())), 1)

    def test_library_groups_legacy_duplicates(self):
        txt, _ = self._pair()
        _, d1 = E.new_job("书")
        E.save_source(d1, "书.txt", txt)
        _, d2 = E.new_job("书")
        E.save_source(d2, "书.txt", txt)
        E.set_progress(d1, 2, 0.5)
        E.set_progress(d2, 5, 0.1)

        books = S.library()
        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["copies"], 2)
        self.assertEqual(books[0]["kinds"], ["txt"])
        self.assertEqual(books[0]["progress"], {"chapter": 5, "ratio": 0.1})

        # 门面固定后，读者往回重读时不能被另一份旧副本的更大章号拽回去。
        canonical = S.job_dir(books[0]["id"])
        E.set_progress(canonical, 1, 0.0)
        self.assertEqual(S.library()[0]["progress"], {"chapter": 1, "ratio": 0.0})


class TestReviewBridge(WorkCase):
    def test_review_passes_budget_and_seed_not_fuzzy_band(self):
        d = self.make_job()
        seen = {}

        def fake(args, timeout=0, env=None, job=None):
            seen["args"] = args
            return True, '{"asked": 0}'

        with mock.patch.object(E, "_run", side_effect=fake):
            ok, _, stats = E.review(d, "http://example/v1", "stub",
                                    "secret", budget=800, seed=7)
        self.assertTrue(ok)
        self.assertEqual(stats["asked"], 0)
        self.assertIn("--max", seen["args"])
        self.assertEqual(seen["args"][seen["args"].index("--max") + 1], "800")
        self.assertIn("--seed", seen["args"])
        self.assertEqual(seen["args"][seen["args"].index("--seed") + 1], "7")
        self.assertNotIn("--lo", seen["args"])
        self.assertNotIn("secret", seen["args"])


class TestKeepPersistence(WorkCase):
    def test_keep_writes_analyzed_and_sidecar_and_reinjects(self):
        d = self.make_job()
        out = d / "out"
        out.mkdir(exist_ok=True)
        doc = {
            "chapters": [{
                "index": 1, "title": "第一章",
                "paragraphs": [{
                    "pid": "c1p1", "kind": "body", "text": "正文",
                    "score": 0.55, "keep": False,
                }],
            }]
        }
        (out / "analyzed.json").write_text(json.dumps(doc), encoding="utf-8")
        page = out / "ch0001.html"
        page.write_text("<html><body><p class=\"p\">正文</p></body></html>",
                        encoding="utf-8")

        self.assertEqual(E.set_keep(d, "ch0001.html", 0, True), "c1p1")
        saved = json.loads((out / "analyzed.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["chapters"][0]["paragraphs"][0]["keep"])
        self.assertTrue(json.loads((d / "keeps.json").read_text(encoding="utf-8"))["c1p1"])

        E._inject_keep_bridge(d, out)
        html = page.read_text(encoding="utf-8")
        self.assertIn("data-qs-keep-bridge", html)
        self.assertIn("__qsInitialKeeps=[true]", html)

        saved["chapters"][0]["paragraphs"][0]["keep"] = False
        (out / "analyzed.json").write_text(json.dumps(saved), encoding="utf-8")
        E._apply_keep_sidecar(d)
        restored = json.loads((out / "analyzed.json").read_text(encoding="utf-8"))
        self.assertTrue(restored["chapters"][0]["paragraphs"][0]["keep"])

    def test_keep_index_skips_separator_markup(self):
        d = self.make_job()
        out = d / "out"
        out.mkdir(exist_ok=True)
        doc = {"chapters": [{"index": 1, "title": "第一章", "paragraphs": [
            {"pid": "sep", "kind": "separator", "text": "---", "score": 0.0},
            {"pid": "body1", "kind": "body", "text": "正文", "score": 0.55},
        ]}]}
        (out / "analyzed.json").write_text(json.dumps(doc), encoding="utf-8")
        E.set_keep(d, "ch0001.html", 0, True)
        saved = json.loads((out / "analyzed.json").read_text(encoding="utf-8"))
        self.assertFalse(saved["chapters"][0]["paragraphs"][0].get("keep", False))
        self.assertTrue(saved["chapters"][0]["paragraphs"][1]["keep"])


class TestAndroidBudget(unittest.TestCase):
    def test_android_uses_same_total_budget_and_batch(self):
        text = (ROOT / "android" / "app" / "src" / "main" / "java" /
                "com" / "qianshen" / "app" / "MainActivity.kt").read_text(
                    encoding="utf-8")
        self.assertIn("const val AI_MAX = 800", text)
        self.assertIn("const val AI_BATCH = 16", text)
        self.assertIn("const val AI_CONCURRENCY = 3", text)


class TestAndroidReaderIsolation(unittest.TestCase):
    """阅读器直接添加的书只进阅读器，不能再借工作台的 stage / currentJob 串线。"""

    def test_source_meta_always_parses_the_normalized_txt(self):
        runner = (ROOT / "android" / "app" / "src" / "main" / "python" /
                  "runner.py").read_text(encoding="utf-8")
        self.assertIn("bookread.read_book(p.read_bytes(), p.name)", runner)
        self.assertNotIn("bookread.read_book(p.read_bytes(), name or p.name)", runner)

    def test_reader_has_separate_state_and_back_target(self):
        ui = (ROOT / "android" / "app" / "src" / "main" / "java" /
              "com" / "qianshen" / "app" / "MainActivity.kt").read_text(
                  encoding="utf-8")
        self.assertIn("private var readerStage: Stage", ui)
        self.assertIn("private var readerJob: String", ui)
        self.assertIn("stage = readerStage", ui)
        self.assertIn("Page.Reader -> backFromReader()", ui)
        self.assertIn('callAttr("source_meta", src.absolutePath, src.name)', ui)


class TestAndroidShelfStates(unittest.TestCase):
    """书架要分清加载 / 失败 / 空，导出按钮要认自己那一份文件。"""

    def test_shelf_has_distinct_loading_and_error_state(self):
        ui = (ROOT / "android" / "app" / "src" / "main" / "java" /
              "com" / "qianshen" / "app" / "MainActivity.kt").read_text(
                  encoding="utf-8")
        self.assertIn("private var shelfLoading: Boolean", ui)
        self.assertIn("private var shelfError: String", ui)
        self.assertIn("if (loading && books.isEmpty())", ui)
        self.assertIn("if (error.isNotBlank())", ui)
        self.assertIn("QsButton(\"重试\", onReload", ui)

    def test_export_callback_carries_the_clicked_filename(self):
        ui = (ROOT / "android" / "app" / "src" / "main" / "java" /
              "com" / "qianshen" / "app" / "MainActivity.kt").read_text(
                  encoding="utf-8")
        self.assertIn("onExport: (ShelfBook, String) -> Unit", ui)
        self.assertIn("onExport(b, f)", ui)
        self.assertIn("private fun exportFromShelf(book: ShelfBook, fileName: String)", ui)
        self.assertNotIn("book.files.firstOrNull()?.let", ui)

    def test_android_export_strips_original_epub_suffix(self):
        ui = (ROOT / "android" / "app" / "src" / "main" / "java" /
              "com" / "qianshen" / "app" / "MainActivity.kt").read_text(
                  encoding="utf-8")
        self.assertIn('Regex("""\\.(txt|epub)$""", RegexOption.IGNORE_CASE)', ui)


class TestAndroidMergedProgress(unittest.TestCase):
    """合并历史副本时，门面书没进度要迁入最远位置；已有进度不能被旧副本覆盖。"""

    def test_canonical_adopts_best_progress_only_when_missing(self):
        ui = (ROOT / "android" / "app" / "src" / "main" / "java" /
              "com" / "qianshen" / "app" / "MainActivity.kt").read_text(
                  encoding="utf-8")
        self.assertIn("adoptBestProgress(canonical, ds)", ui)
        self.assertIn("private fun bestShelfProgress(ds: List<File>): JSONObject?", ui)
        self.assertIn("if (own != null && own.optInt(\"chapter\", -1) >= 0) return", ui)
        self.assertIn("meta.put(\"progress\", best)", ui)


class TestAndroidAiMandatory(unittest.TestCase):
    """analyzed.json 不等于 AI 已完成；工作台不能复用旧结果或绕过导出闸门。"""

    def test_ai_success_marker_is_written_after_review_and_render(self):
        runner = (ROOT / "android" / "app" / "src" / "main" / "python" /
                  "runner.py").read_text(encoding="utf-8")
        self.assertIn('marker = src_dir / "ai_ok.json"', runner)
        self.assertIn('"analyzed_sha256": hashlib.sha256(analyzed.read_bytes()).hexdigest()', runner)

    def test_workbench_requires_current_ai_marker(self):
        ui = (ROOT / "android" / "app" / "src" / "main" / "java" /
              "com" / "qianshen" / "app" / "MainActivity.kt").read_text(
                  encoding="utf-8")
        self.assertIn("private fun aiReady(dir: File): Boolean", ui)
        self.assertIn("if (aiReady(target))", ui)
        self.assertIn("clearAiReady(dir)", ui)
        self.assertIn('require(aiReady(dir)) { "AI 复核还没完整跑完', ui)
        self.assertIn("enabled = hasOut && hasAi", ui)

    def test_upload_confirmation_stays_on_workbench(self):
        ui = (ROOT / "android" / "app" / "src" / "main" / "java" /
              "com" / "qianshen" / "app" / "MainActivity.kt").read_text(
                  encoding="utf-8")
        start = ui.index("private fun runDoctor")
        end = ui.index("// ---------- 整本", start)
        self.assertNotIn("goToPage(Page.Reader)", ui[start:end])


class TestAndroidReaderToc(unittest.TestCase):
    """阅读器要有目录入口，目录页不能覆盖正文章节进度。"""

    def test_reader_opens_generated_toc_page(self):
        ui = (ROOT / "android" / "app" / "src" / "main" / "java" /
              "com" / "qianshen" / "app" / "MainActivity.kt").read_text(
                  encoding="utf-8")
        self.assertIn("private fun openReaderToc()", ui)
        self.assertIn('QsButton("目录", onToc', ui)
        self.assertIn("readerStage = s.copy(html = toc, version = s.version + 1)", ui)
        self.assertIn("page !in chapterFiles", ui)


class TestRenderTarget(WorkCase):
    """复核后的重渲必须写回 build 实际使用的路径，不能生成 out/index/。"""

    def test_split_target_is_directory(self):
        d = self.make_job()
        out = d / "out"
        out.mkdir(exist_ok=True)
        (out / "index.html").write_text("old", encoding="utf-8")
        seen = {}

        def fake(args, timeout=0, env=None, job=None):
            seen["args"] = args
            return True, "ok"

        with mock.patch.object(E, "_run", side_effect=fake):
            E.render(d)
        target = Path(seen["args"][seen["args"].index("-o") + 1])
        self.assertEqual(target, out)

    def test_single_target_is_book_html(self):
        d = self.make_job()
        out = d / "out"
        out.mkdir(exist_ok=True)
        (out / "book.html").write_text("old", encoding="utf-8")
        seen = {}

        def fake(args, timeout=0, env=None, job=None):
            seen["args"] = args
            return True, "ok"

        with mock.patch.object(E, "_run", side_effect=fake):
            E.render(d)
        target = Path(seen["args"][seen["args"].index("-o") + 1])
        self.assertEqual(target, out / "book.html")


# ---------- app/server.py ----------

class TestJobPath(WorkCase):
    """job_dir 是整个后端唯一把外部输入拼进路径的地方，越界必须挡住。"""

    def test_accepts_real_job(self):
        d = self.make_job()
        self.assertEqual(S.job_dir(d.name), d.resolve())

    def test_rejects_traversal(self):
        for bad in ("..", "../app", "..\\app", "app", ""):
            with self.assertRaises(FileNotFoundError, msg=bad):
                S.job_dir(bad)

    def test_rejects_missing(self):
        with self.assertRaises(FileNotFoundError):
            S.job_dir("20200101-000000-zzzzz")


class TestPhaseOnDisk(WorkCase):
    """服务重启后内存状态就没了，靠磁盘产物推断走到哪一步。"""

    def test_idle(self):
        self.assertEqual(S.phase_on_disk(self.make_job()), "idle")

    def test_checked(self):
        d = self.make_job()
        (d / "confirm").mkdir()
        (d / "confirm" / "book.html").write_text("x", encoding="utf-8")
        self.assertEqual(S.phase_on_disk(d), "checked")

    def test_ready(self):
        d = self.make_job()
        (d / "confirm").mkdir()
        (d / "confirm" / "book.html").write_text("x", encoding="utf-8")
        (E.out_of(d) / "book.html").write_text("x", encoding="utf-8")
        self.assertEqual(S.phase_on_disk(d), "ready")


class TestListJobs(WorkCase):
    """「以前的书」的数据源：新的在前，不是任务的目录要跳过。"""

    def test_newest_first(self):
        self.make_job("老书")
        b = self.make_job("新书")
        meta = json.loads((b / "meta.json").read_text(encoding="utf-8"))
        meta["created"] += 100
        (b / "meta.json").write_text(json.dumps(meta, ensure_ascii=False),
                                     encoding="utf-8")
        self.assertEqual([it["name"] for it in S.list_jobs()], ["新书", "老书"])

    def test_skips_dirs_without_meta(self):
        self.make_job("正经书")
        (self.work / "随手建的").mkdir()
        self.assertEqual([it["name"] for it in S.list_jobs()], ["正经书"])

    def test_item_shape(self):
        self.make_job("书")
        self.assertEqual(sorted(S.list_jobs()[0]),
                         ["created", "job", "name", "phase"])


# ---------- app/bookread.py ----------

class TestSplitChapters(unittest.TestCase):
    """阅读器按章号切段，纯解析，不碰引擎。"""

    def test_paras_drops_blanks(self):
        self.assertEqual(B.to_paras("　第一章　\n\n  正文  \n"), ["第一章", "正文"])

    def test_splits_on_marks(self):
        ch = B.split_chapters(["第1章 开端", "甲", "乙", "第2章 结尾", "丙"])
        self.assertEqual([c["title"] for c in ch], ["第1章 开端", "第2章 结尾"])
        self.assertEqual(ch[0]["paras"], ["甲", "乙"])

    def test_leading_text_becomes_opening(self):
        ch = B.split_chapters(["楔子", "第一章 起", "正文"])
        self.assertEqual(ch[0]["title"], "开头")
        self.assertEqual(ch[0]["paras"], ["楔子"])

    def test_no_marks_is_one_chapter(self):
        ch = B.split_chapters(["没有章号", "也没有"])
        self.assertEqual(len(ch), 1)
        self.assertEqual(ch[0]["title"], "全文")

    def test_mark_variants(self):
        for title in ("第1章", "第一百二十三章", "Chapter 3", "Part 2", "12、"):  # noqa: E501
            self.assertEqual(B.split_chapters([title, "正文"])[0]["title"], title)


class TestReadTxt(unittest.TestCase):
    def test_decodes_gb18030(self):
        book = B.read_txt("第一章 测\n\n正文一段。".encode("gb18030"))
        self.assertEqual(book["kind"], "txt")
        self.assertEqual(book["chapters"][0]["title"], "第一章 测")

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            B.read_txt("   \n\n  ".encode("utf-8"))


class TestReadEpub(unittest.TestCase):
    """EPUB 就是个 zip：按书脊顺序取每一节的正文。"""

    def test_chapters_in_spine_order(self):
        raw = make_epub([("第一章 起", ["甲", "乙"]), ("第二章 落", ["丙"])])
        book = B.read_book(raw, "t.epub")
        self.assertEqual(book["kind"], "epub")
        self.assertEqual(book["title"], "测试书")
        self.assertEqual([c["title"] for c in book["chapters"]],
                         ["第一章 起", "第二章 落"])
        self.assertEqual(book["chapters"][0]["paras"], ["甲", "乙"])

    def test_detects_zip_magic(self):
        """扩展名不对，但文件头是 PK，也当 EPUB。"""
        self.assertEqual(B.read_book(make_epub([("一", ["甲"])]), "没扩展名")["kind"],
                         "epub")

    def test_rejects_without_opf(self):
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
        with self.assertRaises(ValueError):
            B.read_epub(buf.getvalue())


class TestToTxt(unittest.TestCase):
    """EPUB 摊平成 TXT：标题必须像章号，否则引擎会把整本当一章。"""

    def test_keeps_chapter_like_title(self):
        txt = B.to_txt({"chapters": [{"title": "第3章 试探", "paras": ["甲"]}]})
        self.assertTrue(txt.startswith("第3章 试探\n"))

    def test_renames_odd_title(self):
        txt = B.to_txt({"chapters": [{"title": "楔子", "paras": ["甲"]},
                                     {"title": "尾声", "paras": ["乙"]}]})
        self.assertIn("第1章\n", txt)
        self.assertIn("第2章\n", txt)
        self.assertNotIn("楔子", txt)

    def test_renames_overlong_title(self):
        txt = B.to_txt({"chapters": [{"title": "长" * 41, "paras": ["甲"]}]})
        self.assertIn("第1章\n", txt)

    def test_blank_line_between_paras(self):
        self.assertEqual(
            B.to_txt({"chapters": [{"title": "第1章", "paras": ["甲", "乙"]}]}),
            "第1章\n\n甲\n\n乙\n")


if __name__ == "__main__":
    unittest.main()
