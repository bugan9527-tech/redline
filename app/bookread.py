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

# 分层：引擎导出时会把这些标记写进 XHTML（class，或用颜色/透明度表达），阅读器要能读回来。
# 注意区分两件事：PLAIN 是"这本书没写过任何分层"（原样显示，不压深浅）；
# MID 是引擎口中的「普通」档——引擎对普通档不写任何标记，所以未标记段落在引擎书里就是 MID。
TIERS = ("plot", "mid", "weak", "fade", "zero")
TIER_DIM = {"plot": 0.0, "mid": 0.10, "weak": 0.28, "fade": 0.58, "zero": 0.82}
MID = "mid"
PLAIN = "plain"
DEFAULT_STRENGTH = 0.55

_STRENGTH_RE = re.compile(r"强度\s*([0-9]*\.?[0-9]+)")
_DECL_RE = re.compile(r"([a-zA-Z-]+)\s*:\s*([^;]+)")
_HEX_RE = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})")
_RGB_RE = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")


def strength_of(description: str, default: float = DEFAULT_STRENGTH) -> float:
    """导出文件在简介里写了强度（`… 强度 0.55 …`）；读不到就按引擎默认档算。"""
    m = _STRENGTH_RE.search(description or "")
    try:
        return max(0.05, min(1.0, float(m.group(1)))) if m else default
    except ValueError:
        return default


def _tier_of_dim(dim: float) -> str:
    return min(TIERS, key=lambda t: abs(TIER_DIM[t] - dim))


def _tier_of_alpha(alpha: float, strength: float) -> str:
    return _tier_of_dim((1.0 - alpha) / max(0.05, strength))


def _gray(color: str) -> int | None:
    """分层用的颜色一定是灰的；彩色文字（比如正文自身的墨色）不算分层标记。"""
    m = _HEX_RE.search(color or "")
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    else:
        m = _RGB_RE.search(color or "")
        if not m:
            return None
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return (r + g + b) // 3 if max(r, g, b) - min(r, g, b) <= 6 else None


def _decls(style: str) -> dict[str, str]:
    return {k.strip().lower(): v.strip() for k, v in _DECL_RE.findall(style or "")}


def tier_of_decls(cls: str = "", style: str = "",
                  strength: float = DEFAULT_STRENGTH) -> str:
    """从 class / 行内 style 反解档位：class 优先，其次加粗、透明度、灰色。没有标记就是 PLAIN。"""
    m = re.search(r"\bt-(plot|mid|weak|fade|zero)\b", cls or "")
    if m:
        return m.group(1)
    d = _decls(style)
    if d.get("font-weight", "").lower() in ("600", "700", "bold", "800", "900"):
        return "plot"
    if "opacity" in d:
        try:
            return _tier_of_alpha(float(d["opacity"]), strength)
        except ValueError:
            return PLAIN
    if "color" in d:
        g = _gray(d["color"])
        if g is not None:
            # 引擎把不透明度换算成"混到纸白"的灰：g = 255 - alpha × 204
            alpha = (255 - g) / 204.0
            return "plot" if alpha >= 1.0 else _tier_of_alpha(alpha, strength)
    return PLAIN


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


def split_chapters(paras: list[str], tiers: list[str] | None = None) -> list[dict]:
    """按章节标题行切；没有标题就整本当一章。

    tiers 与 paras 一一对应；切到哪一段就跟着带到哪一章，不会错位。
    """
    lv = list(tiers) if tiers and len(tiers) == len(paras) else [PLAIN] * len(paras)

    def cut(start: int, end: int) -> dict:
        return {"paras": paras[start:end], "tiers": lv[start:end]}

    marks = [i for i, p in enumerate(paras) if len(p) <= 40 and _CHAPTER.match(p)]
    if not marks:
        return [{"title": "全文", **cut(0, len(paras))}]
    chapters = []
    if marks[0] > 0:
        chapters.append({"title": "开头", **cut(0, marks[0])})
    for n, start in enumerate(marks):
        end = marks[n + 1] if n + 1 < len(marks) else len(paras)
        title = paras[start]
        body = cut(start + 1, end)
        if body["paras"] or title:
            chapters.append({"title": title, **body})
    return chapters


def read_txt(raw: bytes) -> dict:
    text = decode(raw)
    paras = to_paras(text)
    if not paras:
        raise ValueError("这本书里没有可读的文字。")
    chapters = split_chapters(paras)
    return {"kind": "txt", "title": "", "chapters": chapters, "chars": len(text)}


class _Blocks(HTMLParser):
    """把一小节 XHTML 拆成块级段落；script/style/head 丢掉。

    引擎导出的 EPUB 把深浅写在标签上（`<p class="t-fade" style="color:#747474">`），
    所以这里不能只看文字——每段还要记下它落在哪一层带了标记的块级标签上。
    """

    _SKIP = {"script", "style", "head"}
    _BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "article", "blockquote", "tr", "td", "pre", "figure", "figcaption"}
    _HEAD = {"h1", "h2", "h3", "title"}

    def __init__(self, strength: float = DEFAULT_STRENGTH) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, bool, str]] = []   # (text, is_heading, tier)
        self._buf: list[str] = []
        self._skip = 0
        self._head = 0
        self._open: list[tuple[str, str, str]] = []     # (tag, class, style)
        self._strength = strength

    def _markup(self) -> tuple[str, str]:
        """最近的、带了 class/style 的那层块级标签——淡化样式有时挂在外层 div 上。"""
        for _tag, cls, style in reversed(self._open):
            if cls or style:
                return cls, style
        return "", ""

    def _flush(self) -> None:
        if self._buf:
            cls, style = self._markup()
            self.blocks.append((" ".join(self._buf), bool(self._head),
                                tier_of_decls(cls, style, self._strength)))
            self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
            return
        if tag in self._BLOCK:
            self._flush()
            d = dict(attrs)
            self._open.append((tag, d.get("class") or "", d.get("style") or ""))
        if tag in self._HEAD:
            self._head = 1

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag in self._BLOCK:
            self._flush()
            for i in range(len(self._open) - 1, -1, -1):
                if self._open[i][0] == tag:
                    del self._open[i:]
                    break
        if tag in self._HEAD:
            self._head = 0

    def handle_data(self, data):
        if self._skip:
            return
        s = data.strip()
        if s:
            self._buf.append(s)

    def finish(self) -> list[tuple[str, bool, str]]:
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
        book_desc = ""          # 导出时把强度写在这里，反解灰阶要按它算
        for el in opf.iter():
            name = _local(el.tag)
            if name == "item" and el.get("id") and el.get("href"):
                manifest[el.get("id")] = el.get("href")
            elif name == "itemref" and el.get("idref"):
                spine.append(el.get("idref"))
            elif name == "title" and (el.text or "").strip():
                book_title = el.text.strip()
            elif name == "description" and (el.text or "").strip():
                book_desc = el.text.strip()

        strength = strength_of(book_desc)

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

            parser = _Blocks(strength)
            parser.feed(decode(data))
            parser.close()
            blocks = parser.finish()
            if not blocks:
                continue
            head = next((t for t, is_head, _ in blocks if is_head), "")
            body = [(t, tier) for t, is_head, tier in blocks if not is_head]
            paras = [t for t, _ in body]
            if not paras and not head:
                continue
            chapters.append({"title": head or "", "paras": paras,
                             "tiers": [tier for _, tier in body]})

        if not chapters:
            raise ValueError("这个 EPUB 里没有可读的正文。")

        # 只要这本书确实带了分层标记，没被标记的段落就该算「普通」档——
        # 引擎对普通档不写标记，这是它表示"普通"的方式。整本都没标记（普通 EPUB / TXT）
        # 就一路 PLAIN 到底，别把没判过的书平白压浅。
        if any(t != PLAIN for ch in chapters for t in ch["tiers"]):
            for ch in chapters:
                ch["tiers"] = [MID if t == PLAIN else t for t in ch["tiers"]]

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
