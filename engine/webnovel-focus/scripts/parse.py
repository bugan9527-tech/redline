"""把网文文本解析成「章节 / 段落 / 字符偏移」的中立中间结果。

这份结果是载体无关的：HTML 渲染、EPUB 生成、浏览器注入广告拦截器式
覆盖，都消费同一份数据。所以解析层只需要做对一次。

用法:
    python parse.py <输入文本> [-o 输出.json] [--limit N] [--from N]

--limit N  只取前 N 章（按章增量处理时用得上）
--from N   从第 N 章开始取
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CHAPTER_RE = re.compile(
    r"^(?P<ind>[ \t\u3000]*)(?P<title>第[零一二三四五六七八九十百千万两0-9０-９]+[章回节卷篇]"
    r"[^\n]{0,50})(?P<tail>[ \t\u3000]*)$",
    re.MULTILINE,
)

# 一本书的章节标题是顶格还是缩进，是这本书自己的惯例。有的书正文段缩进 4 空格，
# 正文里偶尔会出现整行以「第X章」开头的句子（「第五章的章节名为【狂饮】」，
# 盗版源还会在章末重复贴一次标题），这些行一旦被当成章节头，一章就被切成碎片。
# 所以先统计本书惯例：标题几乎全部顶格时才丢掉缩进的候选。
FLAT_MIN = 3
FLAT_DOMINANCE = 0.9

# 正文里一句「第三章是暴不动了」（实为作者通知里的一行）整行看着就是标题。
# 单看这一行无法与真标题区分，所以加一道序列判据：只有「章号与上下文不连续，
# 且紧接着的一行就恢复到上一章 +1」时才判它是正文——真回退（新卷重启）不会
# 下一行就接回原序列。详见 references/judgment-rules.md 的「源文件陷阱」。
HEAD_NUM_RE = re.compile(r"^第([零〇一二两三四五六七八九十百千万0-9０-９]+)[章回节卷篇]")
SUSPECT_HEAD_RE = re.compile(r"^第[零〇一二两三四五六七八九十百千万0-9０-９]+[章回节卷篇](的|是|点|，|,)")
CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNITS = {"十": 10, "百": 100, "千": 1000}
CN_MYRIAD = 10000


def head_number(title: str) -> int | None:
    """「一千二百二十四」-> 1224。只处理到万，章号够用。"""
    m = HEAD_NUM_RE.match(title or "")
    if not m:
        return None
    raw = m.group(1)
    if raw.isdigit():
        return int(raw)
    total = num = 0
    for ch in raw:
        if ch in CN_DIGITS:
            num = CN_DIGITS[ch]
        elif ch in CN_UNITS:
            total += (num or 1) * CN_UNITS[ch]
            num = 0
        elif ch == "万":
            total = (total + num) * CN_MYRIAD
            num = 0
        else:
            return None
    return total + num


def _drop_sequence_anomalies(
    marks: list[tuple[int, int, str]]
) -> tuple[list[tuple[int, int, str]], list[tuple[int, int, str]]]:
    """丢掉「章号与上下文不连续、下一行立刻接回上一章 +1」的疑似正文行。"""
    kept: list[tuple[int, int, str]] = []
    dropped: list[tuple[int, int, str]] = []
    for i, mark in enumerate(marks):
        title = mark[2]
        nxt = head_number(marks[i + 1][2]) if i + 1 < len(marks) else None
        prev = head_number(kept[-1][2]) if kept else None
        cur = head_number(title)
        if (
            SUSPECT_HEAD_RE.match(title)
            and cur is not None and prev is not None and nxt is not None
            and cur != prev + 1 and nxt == prev + 1
        ):
            dropped.append(mark)
            continue
        kept.append(mark)
    return kept, dropped

BLANK = " \t\u3000\r"


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start] in BLANK:
        start += 1
    while end > start and text[end - 1] in BLANK:
        end -= 1
    return start, end


def find_head_marks(
    text: str,
) -> tuple[list[tuple[int, int, str]], list[tuple[int, int, str, str]]]:
    """章节头候选 -> (采用的, 丢掉的)。

    丢掉的都是「整行长得像标题、实际不是标题」的行，两种情况：本书标题顶格而正文
    缩进时，缩进的整行要么是正文句子（「第三节点，楼兰古墓，太阳王！」），要么是
    盗版源在章末重复贴的标题；顶格的则要章号序列同时佐证（「第三章是暴不动了」）。
    每项带一个 why，体检单里要原样报给用户。
    """
    cands = [
        (m.start(), m.end(), m.group("title").strip(), bool(m.group("ind")))
        for m in CHAPTER_RE.finditer(text)
    ]
    flat = [c for c in cands if not c[3]]
    indented = [c for c in cands if c[3]]
    if len(flat) >= FLAT_MIN and len(flat) >= FLAT_DOMINANCE * len(cands):
        kept = [(s, e, t) for s, e, t, _ in flat]
        dropped = [(s, e, t, "整行缩进，本书标题一律顶格") for s, e, t, _ in indented]
    else:
        kept, dropped = [(s, e, t) for s, e, t, _ in cands], []
    kept, seq_dropped = _drop_sequence_anomalies(kept)
    dropped += [(s, e, t, "章号与上下文不连续，下一行即接回上一章 +1") for s, e, t in seq_dropped]
    return kept, dropped


def split_chapters_ex(text: str) -> tuple[list[dict], list[tuple[int, int, str, str]]]:
    """同 split_chapters，另外把被丢掉的缩进标题行也返回给调用方（体检要报出来）。"""
    marks, dropped = find_head_marks(text)
    chapters: list[dict] = []

    if marks and text[: marks[0][0]].strip():
        chapters.append({"index": 0, "title": "（前言）", "start": 0, "end": marks[0][0], "body_start": 0})

    if not marks:
        chapters.append({"index": 1, "title": "", "start": 0, "end": len(text), "body_start": 0})
        return chapters, dropped

    for i, (mstart, mend, title) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        chapters.append(
            {"index": i + 1, "title": title, "start": mstart, "end": end, "body_start": mend}
        )
    return chapters, dropped


def split_chapters(text: str) -> list[dict]:
    return split_chapters_ex(text)[0]


def split_paragraphs(text: str, start: int, end: int) -> list[dict]:
    paras: list[dict] = []
    for m in re.finditer(r"[^\n]+", text[start:end]):
        s, e = _trim(text, start + m.start(), start + m.end())
        if s >= e:
            continue
        paras.append({"start": s, "end": e, "text": text[s:e]})
    return paras


def parse_text(text: str, source: str = "", limit: int | None = None, start_at: int = 1) -> dict:
    chapters = []
    for ch in split_chapters(text):
        if ch["index"] < start_at:
            continue
        if limit is not None and len(chapters) >= limit:
            break
        paras = split_paragraphs(text, ch["body_start"], ch["end"])
        for i, p in enumerate(paras):
            p["pid"] = f"c{ch['index']}p{i}"
            p["kind"] = "body"
            p["score"] = None
            p["flags"] = []
            p["anchors"] = []
            p["evidence"] = []
        chapters.append({**ch, "paragraphs": paras})
    return {
        "source": source,
        "char_count": len(text),
        "chapters": chapters,
    }


def parse_file(path: str | Path, limit: int | None = None, start_at: int = 1) -> dict:
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return parse_text(text, source=p.name, limit=limit, start_at=start_at)


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: parse.py <input.txt> [-o out.json] [--limit N] [--from N]")
        raise SystemExit(2)

    src = Path(sys.argv[1])
    out = None
    if "-o" in sys.argv:
        out = Path(sys.argv[sys.argv.index("-o") + 1])
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
    start_at = int(sys.argv[sys.argv.index("--from") + 1]) if "--from" in sys.argv else 1

    doc = parse_file(src, limit=limit, start_at=start_at)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {out} ({len(doc['chapters'])} chapters)")
    else:
        print(json.dumps(doc, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
