"""深浅 · 阅读器：把一本 TXT / EPUB 解成一段段可读的正文。只用 Python 标准库。

它不改写一个字：TXT 按章节标记切，EPUB 是个 zip，按书脊（spine）顺序取出每一节的
正文段落。不联网、不落盘。
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

MAX_BYTES = 80 * 1024 * 1024

_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "utf-16", "big5")

# 章节标题：第X章/节/回/卷/篇/部，Chapter N，Part N，数字、或数字.
_CHAPTER = re.compile(
    r"(?:第[0-9零一二三四五六七八九十百千万两]+[章节回卷篇部]"
    r"|Chapter\s+\d+"
    r"|Part\s+\d+"
    r"|\d+\s*[、.．])"
)


def decode(raw: bytes) -> str:
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def to_paras(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        s = line.strip().strip("\u3000").strip()
        if s:
            out.append(s)
    return out


def split_chapters(paras: list[str]) -> list[dict]:
    """按章节标题行切；没有标题就整本当一章。"""
    marks = [i for i, p in enumerate(paras) if len(p) <= 40 and _CHAPTER.match(p)]
    if not marks:
        return [{"title": "全文", "paras": paras}]
    chapters = []
    if marks[0] > 0:
        chapters.append({"title": "开头", "paras": paras[: marks[0]]})
    for n, start in enumerate(marks):
        end = marks[n + 1] if n + 1 < len(marks) else len(paras)
        body = paras[start + 1 : end]
        title = paras[start]
        if body or title:
            chapters.append({"title": title, "paras": body})
    return chapters


def read_txt(raw: bytes) -> dict:
    text = decode(raw)
    paras = to_paras(text)
    if not paras:
        raise ValueError("这本书里没有可读的文字。")
    chapters = split_chapters(paras)
    return {"kind": "txt", "title": "", "chapters": chapters, "chars": len(text)}


class _Blocks(HTMLParser):
    """把一小节 XHTML 拆成块级段落；script/style/head 丢掉。"""

    _SKIP = {"script", "style", "head"}
    _BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "article", "blockquote", "tr", "td", "pre", "figure", "figcaption"}
    _HEAD = {"h1", "h2", "h3", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, bool]] = []   # (text, is_heading)
        self._buf: list[str] = []
        self._skip = 0
        self._head = 0

    def _flush(self) -> None:
        if self._buf:
            self.blocks.append((" ".join(self._buf), bool(self._head)))
            self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
            return
        if tag in self._BLOCK:
            self._flush()
        if tag in self._HEAD:
            self._head = 1

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag in self._BLOCK:
            self._flush()
        if tag in self._HEAD:
            self._head = 0

    def handle_data(self, data):
        if self._skip:
            return
        s = data.strip()
        if s:
            self._buf.append(s)

    def finish(self) -> list[tuple[str, bool]]:
        self._flush()
        return self.blocks


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def read_epub(raw: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = z.namelist()

        opf_path = None
        try:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            for el in container.iter():
                if _local(el.tag) == "rootfile" and el.get("full-path"):
                    opf_path = el.get("full-path")
                    break
        except Exception:  # noqa: BLE001
            opf_path = None
        if not opf_path:
            opf_path = next((n for n in names if n.lower().endswith(".opf")), None)
        if not opf_path:
            raise ValueError("这个 EPUB 里没有找到书的结构文件（.opf）。")

        base = posixpath.dirname(opf_path)
        opf = ET.fromstring(z.read(opf_path))

        manifest: dict[str, str] = {}
        spine: list[str] = []
        book_title = ""
        for el in opf.iter():
            name = _local(el.tag)
            if name == "item" and el.get("id") and el.get("href"):
                manifest[el.get("id")] = el.get("href")
            elif name == "itemref" and el.get("idref"):
                spine.append(el.get("idref"))
            elif name == "title" and (el.text or "").strip():
                book_title = el.text.strip()

        chapters = []
        for idref in spine:
            href = manifest.get(idref)
            if not href:
                continue
            rel = href.split("#")[0]
            target = posixpath.normpath(posixpath.join(base, rel)) if base else rel
            try:
                data = z.read(target)
            except KeyError:
                hit = next((n for n in names if n.endswith(rel)), None)
                if not hit:
                    continue
                data = z.read(hit)

            parser = _Blocks()
            parser.feed(decode(data))
            parser.close()
            blocks = parser.finish()
            if not blocks:
                continue
            head = next((t for t, is_head in blocks if is_head), "")
            paras = [t for t, is_head in blocks if not is_head]
            if not paras and not head:
                continue
            chapters.append({"title": head or "", "paras": paras})

        if not chapters:
            raise ValueError("这个 EPUB 里没有可读的正文。")
        return {"kind": "epub", "title": book_title, "chapters": chapters,
                "chars": sum(len(p) for c in chapters for p in c["paras"])}


def to_txt(book: dict) -> str:
    """把解好的书摊平成 TXT：章节标题独占一行，段落之间空一行。

    EPUB 的章节标题如果不像章号，就改用「第 N 章」当标记行，
    这样引擎一定能认出章节边界，不会被整本当成一章。
    """
    blocks: list[str] = []
    for i, ch in enumerate(book.get("chapters", [])):
        title = (ch.get("title") or "").strip()
        if not title or len(title) > 40 or not _CHAPTER.match(title):
            title = f"第{i + 1}章"
        blocks.append(title)
        blocks.append("")
        for p in ch.get("paras", []):
            s = (p or "").strip()
            if s:
                blocks.append(s)
                blocks.append("")
    return "\n".join(blocks).strip() + "\n"


def read_book(raw: bytes, name: str = "") -> dict:
    """按扩展名或文件头判断类型，解成 {kind,title,chapters,chars}。"""
    low = (name or "").lower()
    if low.endswith(".epub") or raw[:2] == b"PK":
        return read_epub(raw)
    return read_txt(raw)
