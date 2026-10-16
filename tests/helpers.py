"""深浅 · 测试用的小工具：临时工作目录 + 现造一本书 / 一个 EPUB。

这个文件不是测试，是给 test_units / test_api 共用的小零件。
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import engine as E  # noqa: E402
import server as S  # noqa: E402


class WorkCase(unittest.TestCase):
    """把 WORK 指到临时目录：测试绝不能碰用户真实的 work/。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (E.WORK, S.WORK)
        E.WORK = S.WORK = self.work = Path(self._tmp.name)
        S.STATE.clear()

    def tearDown(self) -> None:
        S.STATE.clear()
        E.WORK, S.WORK = self._orig
        self._tmp.cleanup()

    def make_job(self, name: str = "测试书") -> Path:
        """造一个只有源文件和 meta.json 的空任务，不跑引擎。"""
        _, d = E.new_job(name)
        return d


def make_epub(chapters: list[tuple[str, list[str]]], title: str = "测试书") -> bytes:
    """造一个最小可用的 EPUB：container.xml + content.opf + 每章一个 xhtml。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>'
            '<container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>')
        items, refs = [], []
        for i, (ch_title, paras) in enumerate(chapters, 1):
            body = "".join(f"<p>{p}</p>" for p in paras)
            z.writestr(
                f"OEBPS/ch{i}.xhtml",
                '<?xml version="1.0" encoding="utf-8"?>'
                '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
                f"<title>{ch_title}</title></head>"
                f"<body><h1>{ch_title}</h1>{body}</body></html>")
            items.append(f'<item id="c{i}" href="ch{i}.xhtml" '
                         'media-type="application/xhtml+xml"/>')
            refs.append(f'<itemref idref="c{i}"/>')
        z.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            'unique-identifier="id"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:title>{title}</dc:title><dc:identifier id="id">x</dc:identifier>'
            '</metadata><manifest>' + "".join(items) + '</manifest><spine>'
            + "".join(refs) + '</spine></package>')
    return buf.getvalue()


def sample_txt(chapters: int = 10) -> bytes:
    """一本够引擎体检通过的小书：有章号，段落也有信息量。"""
    lines: list[str] = []
    for i in range(1, chapters + 1):
        lines += [f"第{i}章 夜里的消息", ""]
        lines += [
            "他把手机翻过来扣在桌上，屏幕亮了一下又暗下去。",
            "窗外的风把晾衣绳吹得直响，谁家的狗叫了两声。",
            "「你到底还算不算数。」这句话他在心里过了很多遍。",
            "他数了数钱包里的钱，最后一张一百的还压在身份证下面。",
            "路灯下面站着一个人，看不清脸，只看见烟头一明一灭。",
            "往前走两步，那人就转身进了巷子，脚步很快。",
            "回到屋里，他把门反锁，坐在床沿上等天亮。",
            "天亮以后，什么也没有发生。",
            "",
        ]
    return "\n".join(lines).encode("utf-8")
