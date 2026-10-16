"""源文件体检 + 风格基线：在解析之前先在原文上量一遍。

新书第一次接触时先跑这个。它回答三件事：

1. 文件本身能不能用——编码、BOM、换行、非法字符。
2. 源文件有没有被整体污染——防盗版反义词替换（好->坏、不->是、你->他…）、
   `??` 替换（`——` / `……` 被换成两个问号）。
3. 这本书的风格基线是什么——段落长度分布、对话密度、标点习惯。

第 3 条是引擎的地基：detect_rules 里所有长度类阈值都相对这份基线算，
绝对字数阈值在真书上一定翻车（见 references/judgment-rules.md）。

用法:
    python prescan.py <输入文本> [-o 输出.json] [--normalize OUT]

--normalize OUT  把源文件转成 UTF-8 / LF（无 BOM）另存，供非 UTF-8 的源用
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse as P  # noqa: E402

try:  # Windows 控制台默认可能是 GBK，中文报告要显式走 UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

# 防盗版替换的字符对：正常写法 -> 被替换成的字
PAIRS = (("好", "坏"), ("不", "是"), ("你", "他"), ("上", "下"), ("无", "有"))
SUB_CHARS = "".join(dict.fromkeys(c for pair in PAIRS for c in pair))

# 「坏」是主角判据，但它有大量合法用法（破坏、坏人、坏笑、坏的……）。
# 不剔除这些搭配的话，判据直接失效：《序列玩家》第 783 章有 5 个「坏」，
# 全是「破坏」，会被当成污染章。剔完之后这个数（下称「裸坏」）在两本书上是：
#   干净章：中位 0，最多 4 个（作者真的在写「蠢还是坏」「大坏小坏」）
#   污染章：中位 11 个，4~25 个
# 判据见下，在两本书上零误报零漏报。
LEGIT_BAD_RE = re.compile(
    r"[破损毁]坏"
    r"|坏(?:人|蛋|笑|事|消息|话|处|习惯|毛病|主意|心思|水|脾气|天气|运气|心眼|分子|东西|账|名声|榜样|苗子)"
    r"|(?:好|学|变|弄|搞|吓|累|饿|气|急|忙|宠|惯|带|教|撞|摔|烧|打|砸|使|耍|叫|越|更|最|挺|不|辨|分|认|记|说|道|斗|击|中|毁|朽|败|保|维|修)坏"
    r"|坏(?:了|掉|死)"
)

# 干净章上限：裸坏 4 个 / 占比 0.36；污染章下限：裸坏 4 个 / 占比 0.44
BAD_MIN_HITS = 3
BAD_RATIO = 0.40
# 「不->是」倒挂在单章上噪声很大（有的章就是不用「不」），只做全书级别的旁证。
# 干净书上限 0.64（>=150 次时），被替换的书 0.64~0.85。
INVERT_RATIO = 0.82
INVERT_MIN_HITS = 150

QQ_RE = re.compile(r"\?\?")
DIALOGUE_RE = re.compile("[\u201c\u201d\u300c\u300e]")
BRACKET_RE = re.compile(r"^\s*[【\[]")
ELLIPSIS_CJK_RE = re.compile("[\u2026]{2,}")
ELLIPSIS_DOT_RE = re.compile(r"\.{4,}")
DECOR_RE = re.compile(r"^[^0-9A-Za-z\u4e00-\u9fff]+$")

# 「第X章」后面紧跟否定/解释性的字，多半是正文行碰巧以章号开头
SUSPECT_HEAD_RE = re.compile(r"^第[零〇一二两三四五六七八九十百千万0-9０-９]+[章回节卷篇](的|是|点|，|,)")

CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNITS = {"十": 10, "百": 100, "千": 1000}
NUM_RE = re.compile(r"^第([零〇一二两三四五六七八九十百千万0-9０-９]+)[章回节卷篇]")


def cn_to_int(text: str) -> int | None:
    """「一千二百二十四」-> 1224。只处理到万，章号够用。"""
    text = text.strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = section = num = 0
    for ch in text:
        if ch in CN_DIGITS:
            num = CN_DIGITS[ch]
        elif ch in CN_UNITS:
            section += (num or 1) * CN_UNITS[ch]
            num = 0
        elif ch == "万":
            total += (section + num) * 10000
            section = num = 0
        else:
            return None
    return total + section + num


# ---------------------------------------------------------------- 读文件


def read_source(path: str | Path) -> dict:
    p = Path(path)
    raw = p.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:] if bom else raw

    encoding, text, strict = None, None, True
    try:
        text, encoding = body.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        try:
            text, encoding = body.decode("gb18030"), "gb18030"
        except UnicodeDecodeError:
            text = body.decode("utf-8", errors="replace")
            encoding, strict = "utf-8(替换符)", False

    newline = "crlf" if "\r\n" in text else ("cr" if "\r" in text else "lf")
    return {
        "path": str(p),
        "bytes": len(raw),
        "encoding": encoding,
        "bom": bom,
        "newline": newline,
        "replacement_chars": text.count("\ufffd"),
        "strict_utf8": strict,
        "text": text.replace("\r\n", "\n").replace("\r", "\n"),
    }


# ---------------------------------------------------------------- 污染检测


def pair_stats(text: str) -> dict:
    """每对字符里「被替换侧」的占比。占比相对本书自己的用字，不用绝对阈值。"""
    counts = {c: text.count(c) for c in SUB_CHARS}
    out = {}
    for a, b in PAIRS:
        total = counts[a] + counts[b]
        out[f"{a}>{b}"] = {
            "left": counts[a],
            "right": counts[b],
            "ratio": round(counts[b] / total, 4) if total else 0.0,
        }
    return out


def naked_bad(text: str) -> tuple[int, float]:
    """剔除「破坏 / 坏人 / 坏的」这类合法搭配后，还剩多少个「坏」。"""
    hits = len(re.findall("坏", LEGIT_BAD_RE.sub("", text)))
    good = text.count("好")
    ratio = hits / (hits + good) if hits + good else 0.0
    return hits, round(ratio, 4)


def pollution_scan(chapters: list[dict], text: str) -> dict:
    rows: list[dict] = []
    for ch in chapters:
        body = text[ch["body_start"]: ch["end"]]
        stats = pair_stats(body)
        bad, bad_ratio = naked_bad(body)
        invert_ratio = stats["不>是"]["ratio"]
        polluted = bad >= BAD_MIN_HITS and bad_ratio >= BAD_RATIO
        inverted = (
            stats["不>是"]["left"] + stats["不>是"]["right"] >= INVERT_MIN_HITS
            and invert_ratio >= INVERT_RATIO
        )
        rows.append(
            {
                "index": ch["index"],
                "title": ch["title"],
                "chars": len(body),
                "bad_hits": bad,
                "bad_ratio": bad_ratio,
                "invert_ratio": invert_ratio,
                "inverted": inverted,
                "bad_per_1k": round(1000 * bad / max(1, len(body)), 2),
                "polluted": polluted,
            }
        )

    hits = [r for r in rows if r["polluted"]]
    runs: list[list[int]] = []
    for r in hits:
        if runs and r["index"] == runs[-1][1] + 1:
            runs[-1][1] = r["index"]
        else:
            runs.append([r["index"], r["index"]])

    # 替换是整段整段发生的，不会只在某一个章节里出现。卡在临界值的章（比如
    # 4 个 / 0.36，刚好比阈值低一点）夹在两个确定的污染区间之间时并入，但单独
    # 记下来——它不像其余章节那样硬。
    bridged: list[int] = []
    merged: list[list[int]] = []
    for run in runs:
        if merged and run[0] - merged[-1][1] <= 3:
            bridged.extend(range(merged[-1][1] + 1, run[0]))
            merged[-1][1] = run[1]
        else:
            merged.append(list(run))

    clean = [r for r in rows if not r["polluted"] and r["index"] not in set(bridged)]
    inverted = [r["index"] for r in rows if r["inverted"]]
    first = min([hits[0]["index"]] + bridged) if hits else None
    return {
        "chapters_total": len(rows),
        "polluted_chapters": len(hits),
        "polluted_runs": merged,
        "bridged_chapters": bridged,
        "first_polluted": first,
        "whole_book": bool(rows) and rows[0]["polluted"],
        "clean_bad_ratio_max": max((r["bad_ratio"] for r in clean), default=0.0),
        "clean_bad_hits_max": max((r["bad_hits"] for r in clean), default=0),
        "inverted_chapters": inverted,
        "clean_chapters": [r["index"] for r in clean],
        "worst": sorted(hits, key=lambda r: -r["bad_ratio"])[:5],
    }


# ---------------------------------------------------------------- 结构与基线


def structure_scan(chapters: list[dict], dropped_heads: list | None = None) -> dict:
    nums: list[int | None] = []
    for ch in chapters:
        m = NUM_RE.match(ch["title"] or "")
        nums.append(cn_to_int(m.group(1)) if m else None)

    # 被丢掉的疑似标题行：带字符偏移和原因，方便用户回源文件核对。
    # 它们没有被删掉，只是留在所在章的正文里，不再当章节标题。
    dropped = []
    for start, _end, title, why in (dropped_heads or []):
        dropped.append({"offset": start, "title": title[:40], "why": why})

    resets, jumps, suspect = [], [], []
    for i, ch in enumerate(chapters):
        title = ch["title"] or ""
        if SUSPECT_HEAD_RE.match(title):
            suspect.append({"mark": i + 1, "title": title[:40]})
        if i == 0 or nums[i] is None or nums[i - 1] is None:
            continue
        if nums[i] < nums[i - 1]:
            resets.append({"mark": i + 1, "prev": nums[i - 1], "cur": nums[i], "title": title[:40]})
        elif nums[i] != nums[i - 1] + 1:
            jumps.append({"mark": i + 1, "prev": nums[i - 1], "cur": nums[i]})

    return {
        "chapter_marks": len(chapters),
        "numbered": sum(1 for n in nums if n is not None),
        "resets": resets,
        "jumps": jumps,
        "suspect_heads": suspect,
        "dropped_indented_heads": dropped,
    }


def baseline_scan(chapters: list[dict]) -> dict:
    lens: list[int] = []
    dialogue = bracket = decor = 0
    cjk_ellipsis = dot_ellipsis = 0
    ch_lens: list[int] = []
    for ch in chapters:
        ch_len = 0
        for p in ch["paragraphs"]:
            t = p["text"]
            lens.append(len(t))
            ch_len += len(t)
            if DIALOGUE_RE.search(t):
                dialogue += 1
            if BRACKET_RE.match(t):
                bracket += 1
            if DECOR_RE.match(t):
                decor += 1
            if ELLIPSIS_CJK_RE.search(t):
                cjk_ellipsis += 1
            elif ELLIPSIS_DOT_RE.search(t):
                dot_ellipsis += 1
        ch_lens.append(ch_len)

    lens.sort()
    n = max(1, len(lens))

    def pct(q: float) -> int:
        return lens[min(n - 1, int(n * q))]

    styles = {"全角省略号…": cjk_ellipsis, "半角......": dot_ellipsis}
    ch_sorted = sorted(ch_lens)
    return {
        "paragraphs": n,
        "para_len": {
            "p10": pct(0.10), "p25": pct(0.25), "p50": pct(0.50),
            "p75": pct(0.75), "p90": pct(0.90),
        },
        "short_para_ratio": round(sum(1 for x in lens if x < 22) / n, 3),
        "dialogue_ratio": round(dialogue / n, 3),
        "bracket_line_ratio": round(bracket / n, 3),
        "decor_line_ratio": round(decor / n, 3),
        "ellipsis_style": max(styles, key=styles.get) if any(styles.values()) else "无",
        "chapter_chars_p50": ch_sorted[len(ch_sorted) // 2] if ch_sorted else 0,
        "chapter_chars_p90": ch_sorted[int(len(ch_sorted) * 0.9)] if ch_sorted else 0,
        "total_chars": sum(ch_lens),
    }


def qq_scan(text: str) -> dict:
    lines = [ln for ln in text.split("\n") if QQ_RE.search(ln)]
    hits = len(QQ_RE.findall(text))
    return {
        "hits": hits,
        "lines": len(lines),
        "per_10k_chars": round(hits / max(1, len(text)) * 10000, 1),
        "note": "`??` 是被换掉的 `——` / `……`：判「标点连打」时必须排除 `?`",
    }


# ---------------------------------------------------------------- 汇总


def scan_file(path: str | Path) -> dict:
    src = read_source(path)
    text = src.pop("text")
    raw_chapters, dropped_heads = P.split_chapters_ex(text)
    chapters = [
        {**ch, "paragraphs": P.split_paragraphs(text, ch["body_start"], ch["end"])}
        for ch in raw_chapters
    ]

    doc = {
        **src,
        "char_count": len(text),
        "integrity": {
            "substitution": pollution_scan(chapters, text),
            "question_marks": qq_scan(text),
        },
        "structure": structure_scan(chapters, dropped_heads),
        "baseline": baseline_scan(chapters),
    }
    doc["verdict"] = verdict_of(doc)
    return doc


def verdict_of(doc: dict) -> dict:
    """「能不能直接读 / 先处理什么」的结论与下一步命令。"""
    notes, actions = [], []
    sub = doc["integrity"]["substitution"]

    if doc["replacement_chars"]:
        notes.append(f"文件里有 {doc['replacement_chars']} 个非法字符，编码可能不是 UTF-8")
        actions.append("先把源文件转成 UTF-8：prescan.py <src> --normalize <out>")
    elif not doc["strict_utf8"]:
        notes.append(f"源文件不是 UTF-8（识别为 {doc['encoding']}），parse.py 会读成乱码")
        actions.append("先把源文件转成 UTF-8：prescan.py <src> --normalize <out>")

    if sub["polluted_chapters"]:
        runs = "、".join(f"{a}-{b}" for a, b in sub["polluted_runs"][:6])
        notes.append(
            f"防盗版反义词替换嫌疑：{sub['polluted_chapters']}/{sub['chapters_total']} 章（区间 {runs}）"
        )
        if sub["whole_book"]:
            actions.append("整本都被替换：正文本身是残的，先跟用户确认换源还是照读")
        else:
            actions.append(
                f"第 {sub['first_polluted']} 章起被替换，读到干净部分就加 --limit {sub['first_polluted'] - 1}"
            )
        actions.append("污染区的人称锚点不可信（你↔他被换过），该区间判定要按更保守的口径看")
    elif len(sub["inverted_chapters"]) >= max(3, sub["chapters_total"] * 0.1):
        notes.append(
            f"{len(sub['inverted_chapters'])} 章出现「是/(不+是)」倒挂"
            f"（≥{INVERT_RATIO}），可能是别的替换版本，建议人工看一眼"
        )

    st = doc["structure"]
    if st["resets"]:
        notes.append(f"章号回退 {len(st['resets'])} 处（引用「第 N 章」会有歧义）")
        actions.append("先跟用户确认编号口径，或统一按位置（--from / --limit）处理")
    if st["suspect_heads"]:
        notes.append(f"{len(st['suspect_heads'])} 处疑似正文行被当成章节标题")
        actions.append("抽查时先看章标题列表，必要时先清洗源文件里的这类行")

    qq = doc["integrity"]["question_marks"]
    if qq["hits"]:
        notes.append(f"`??` 替换 {qq['hits']} 处（{qq['lines']} 行）")

    return {"notes": notes, "actions": actions, "ok": not notes}


def format_report(doc: dict) -> str:
    st, bl, sub = doc["structure"], doc["baseline"], doc["integrity"]["substitution"]
    qq = doc["integrity"]["question_marks"]
    out: list[str] = []

    out.append(f"=== 源文件体检 · {Path(doc['path']).name} ===")
    clean_file = doc["strict_utf8"] and not doc["bom"] and not doc["replacement_chars"]
    out.append(
        f"{'[ok]' if clean_file else '[!]'} 编码 {doc['encoding']}"
        f"{' + BOM' if doc['bom'] else ''} · 换行 {doc['newline'].upper()} · "
        f"{doc['char_count']:,} 字 · {st['chapter_marks']} 个章节标记 · {bl['paragraphs']:,} 段"
    )
    if not clean_file:
        detail = []
        if doc["bom"]:
            detail.append("BOM")
        if doc["newline"] != "lf":
            detail.append(doc["newline"].upper() + " 换行")
        if doc["replacement_chars"]:
            detail.append(f"U+FFFD x{doc['replacement_chars']}")
        if not doc["strict_utf8"]:
            detail.append("非 UTF-8")
        out.append("     parse.py 能处理换行与 BOM；要预处理的是：" + "、".join(detail))

    if sub["polluted_chapters"]:
        runs = "、".join(f"{a}-{b}" for a, b in sub["polluted_runs"][:6])
        out.append(
            f"[!] 防盗版反义词替换嫌疑：{sub['polluted_chapters']}/{sub['chapters_total']} 章"
            f"（首章 {sub['first_polluted']}；区间 {runs}）"
        )
        worst = sub["worst"][0] if sub["worst"] else None
        if worst:
            out.append(
                f"     最强证据 第{worst['index']}章：裸坏 {worst['bad_hits']} 个"
                f"（{worst['bad_per_1k']}/千字）· 裸坏占比 {worst['bad_ratio']:.2f}"
                f" · 是/(不+是)={worst['invert_ratio']:.2f}"
            )
        out.append(
            f"     干净章上限：裸坏 {sub['clean_bad_hits_max']} 个 / 占比 "
            f"{sub['clean_bad_ratio_max']:.2f}（判据：裸坏≥{BAD_MIN_HITS} 个且占比≥{BAD_RATIO}）"
        )
        if sub["bridged_chapters"]:
            out.append(
                f"     区间内有 {len(sub['bridged_chapters'])} 章卡在临界值，已按邻近区间并入："
                + "、".join(f"#{i}" for i in sub["bridged_chapters"][:8])
            )
    else:
        out.append(
            f"[ok] 未发现反义词替换痕迹（每章裸坏 ≤ {sub['clean_bad_hits_max']} 个，"
            f"占比 ≤ {sub['clean_bad_ratio_max']:.2f}）"
        )
    if sub["polluted_chapters"] or sub["clean_bad_hits_max"]:
        out.append("     裸坏 = 剔除「破坏 / 坏人 / 坏的…」这类合法搭配后剩下的「坏」个数")

    if qq["hits"]:
        out.append(
            f"[!] `??` 替换 {qq['hits']} 处 / {qq['lines']} 行"
            f"（{qq['per_10k_chars']} 处每万字）：{qq['note']}"
        )
    else:
        out.append("[ok] 未发现 `??` 替换")

    out.append(
        f"{'[!]' if st['resets'] or st['suspect_heads'] else '[ok]'} 章号："
        f"{st['numbered']}/{st['chapter_marks']} 个标记可解析 · "
        f"回退 {len(st['resets'])} · 跳号/重号 {len(st['jumps'])} · "
        f"疑似正文行 {len(st['suspect_heads'])}"
    )
    for r in st["resets"][:4]:
        out.append(f"     标记 #{r['mark']}：第{r['prev']}章 → 第{r['cur']}章 「{r['title']}」")
    for s in st["suspect_heads"][:4]:
        out.append(f"     疑似正文行：#{s['mark']}「{s['title']}」")
    dropped = st.get("dropped_indented_heads") or []
    if dropped:
        out.append(f"     已忽略 {len(dropped)} 行「长得像标题、实际是正文」的行（没有删字，"
                   "仍留在所在章的正文里）")
        for d in dropped[:4]:
            out.append(f"       偏移 {d['offset']}「{d['title']}」— {d['why']}")

    pl = bl["para_len"]
    out.append("")
    out.append("--- 风格基线（引擎的长度阈值都相对它来定）---")
    out.append(
        f"段落长度 p10 {pl['p10']} / p25 {pl['p25']} / p50 {pl['p50']} / "
        f"p75 {pl['p75']} / p90 {pl['p90']} 字"
    )
    out.append(
        f"短于 22 字的段落占 {bl['short_para_ratio']:.0%}"
        "（这个数越大，绝对阈值在这本书上越会误判）"
    )
    out.append(
        f"对话段 {bl['dialogue_ratio']:.0%} · 【】开头 {bl['bracket_line_ratio']:.1%} · "
        f"纯装饰行 {bl['decor_line_ratio']:.1%} · 省略号写法 {bl['ellipsis_style']}"
    )
    out.append(
        f"章字数 中位 {bl['chapter_chars_p50']:,} / p90 {bl['chapter_chars_p90']:,} · "
        f"全书 {bl['total_chars']:,} 字"
    )

    out.append("")
    out.append("--- 结论 ---")
    out.append("可直接进入解析。" if doc["verdict"]["ok"] else "读之前要先处理：")
    for n in doc["verdict"]["notes"]:
        out.append(f"[!] {n}")
    for a in doc["verdict"]["actions"]:
        out.append(f"    -> {a}")
    return "\n".join(out)


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: prescan.py <input.txt> [-o out.json] [--normalize OUT]")
        raise SystemExit(2)

    src = Path(sys.argv[1])
    out = Path(sys.argv[sys.argv.index("-o") + 1]) if "-o" in sys.argv else None
    norm = Path(sys.argv[sys.argv.index("--normalize") + 1]) if "--normalize" in sys.argv else None

    doc = scan_file(src)
    print(format_report(doc))
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {out}")
    if norm:
        text = read_source(src)["text"]
        norm.parent.mkdir(parents=True, exist_ok=True)
        norm.write_text(text, encoding="utf-8")
        print(f"wrote {norm} (UTF-8 / LF)")


if __name__ == "__main__":
    main()
