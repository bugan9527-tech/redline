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
        """AI 复核会重渲成 index.html，两个都在时以它为准。"""
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
    """AI 复核的实时进度：没跑过要给空 dict，不能抛。"""

    def test_empty_when_absent(self):
        self.assertEqual(E.read_progress(self.make_job()), {})


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
