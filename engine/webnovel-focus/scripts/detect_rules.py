"""规则层：识别零信息内容与关键锚点，输出中立的标签和证据。

这一层只回答「这是什么结构、做了什么动作」，不回答「好不好看」。
处置权在渲染层，由读者偏好决定。

设计要点：长度类阈值一律相对「本书自己的风格基线」来定，而不是绝对字数。
有的作者段落中位数 25 字，有的 120 字，同一个绝对阈值必然一边误报一边漏报。

用法:
    python detect_rules.py <parsed.json> [-o 输出.json]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- 模式库

# 作者话头：必须整行就是标签，不能是「说明自己的死因是……」这类正文
NOTE_HEAD_RE = re.compile(
    r"^\s*(上架感言|感言|作者的话|作者说|单章|求月票|求月|求票|求推荐|"
    r"月底|月初|月末|请假|请假条|公告|开个单章|写在前面|完结感言|上架通知)\s*[：:！!]?\s*$"
)
NOTE_KW = re.compile(
    r"月票|追读|订阅|上架|编辑|读者|书友|加更|催更|群号|推荐票|首订|"
    r"收藏|榜单|三江|扑街|厚爱|鸣谢|感言|万字|打赏|投一下|行行好"
)
NOTE_MIN_KW_RATIO = 0.45
NOTE_MAX_GAP = 3
# 作者话头（整行就是标签）本身就是高精度信号：全 60 章 8695 段扫下来只有 3 处命中，
# 零误报。所以可以全章扫，不受深度限制——上架感言这类长块起点可能离章末很远。
NOTE_HEAD_MIN_DENSITY = 0.25

AUTHOR_NOTE_RE = re.compile(
    r"(^|[（(【\s])(ps|p\.s\.)[：:\s]"
    r"|求推荐票|求月票|求收藏|求打赏|求订阅|么么哒|本章完|未完待续"
    r"|感谢.{0,12}的打赏|关注公众号|读者群",
    re.IGNORECASE,
)

WATERMARK_RE = re.compile(
    r"本章未完|点击下一页|请记住本站|手机用户请|最新章节|笔趣阁|天才一秒记住"
    r"|www\.|https?://|扫描二维码"
)

PANEL_RE = re.compile(r"^[【\[].{0,300}[】\]]$")
PANEL_KEYWORDS = (
    "叮", "经验", "技能", "属性", "等级", "境界", "道具", "获得",
    "宿主", "任务", "积分", "商店", "面板", "气血", "魔力",
)

RECAP_RE = re.compile(
    r"上回回顾|上回说到|前情提要|回顾一下|话说回来|且说|书接上回|上一章说到|之前说到"
)

# 用显式码点写，避免弯引号在编辑/传输过程中被吞掉
# \u201c \u201d = “ ”   \u300c \u300e = 「 『
DIALOGUE_RE = re.compile("[\u201c\u201d\u300c\u300e]")
LABEL_ONLY_RE = re.compile(r"^.{1,12}$")
HAS_PUNCT_RE = re.compile(r"[，。；：]")
# 纯符号行：场景分隔线、装饰性省略号。前后既无汉字也无字母数字。
NO_CONTENT_RE = re.compile(r"^[^0-9A-Za-z\u4e00-\u9fff]+$")

SCENERY_WORDS = (
    "暮色", "夜色", "云霞", "天际", "群山", "山峦", "林梢", "树梢", "雾气",
    "溪水", "溪流", "月光", "月色", "余晖", "霞光", "鸟雀", "花香",
    "风声", "落花", "花瓣", "湖面", "星辰", "星河", "朝阳", "夕阳",
    "清风", "寒风", "炊烟", "露珠", "涟漪", "薄雾", "草坪", "野地",
)

# 省略号的各种写法。作者用哪种是风格问题，不是灌水信号。
ELLIPSIS_RE = re.compile(r"[.。…·]{2,}")
SEPARATOR_RE = re.compile(r"[-—_=*~]{3,}")

# 人名候选必须至少几次出现在「说话 / 动作」动词前面，否则「严重」「任务」
# 这类常见词会被姓氏表误抓成人名。
NAME_VERBS = (
    "说", "道", "问", "答", "笑", "看", "想", "走", "来", "点", "叹", "愣",
    "摇", "低", "抬", "坐", "站", "沉默", "开口", "回头", "转身", "皱眉",
    "盯", "望", "听", "点头", "摇头", "跑", "拉",
)

TURN_WORDS = ("突然", "忽然", "然而", "没想到", "原来", "竟", "终于", "却", "可是", "但")

# 旁白 / 设定解说腔。这类文字常含「死」「危险」等事件词，但只是在讲道理，
# 没有推进任何具体剧情，靠锚点词会被误判成情节。
EXPOSITION_MARKERS = (
    "可以说", "一般来说", "通常", "毕竟", "要知道", "据说", "很多人", "不少人",
    "大多数", "往往", "也就是说", "换句话说", "总而言之", "众所周知", "事实上",
    "所谓", "只是", "但凡", "无论如何",
)
EVENT_WORDS = (
    "封印", "背叛", "突破", "觉醒", "临终", "遗言", "真相", "秘密",
    "陷阱", "重生", "复仇", "陨落", "灭门", "身亡", "回溯",
)
WEAK_EVENT_WORDS = ("死", "杀", "血", "断", "消失", "离开", "父亲", "母亲")
TIME_JUMP_RE = re.compile(
    r"(十|百|千|[零一二三四五六七八九十百千万0-9]+)年前|([三四五六七八九十两0-9]+)年后|当年|那天"
)

SURNAMES = set(
    "赵钱孙李周吴郑王冯陈蒋沈韩杨朱秦许何吕施张孔曹严华金魏陶姜谢邹苏潘葛范彭"
    "鲁韦马方任袁柳唐罗薛伍余姚孟顾萧尹江钟卢莫房贾丁邓洪包崔石龚程邢裴陆荣翁"
    "封郝白康毛邱史侯邵龙万段雷汤黎易常武乔贺赖林徐高夏蔡田樊胡凌霍虞柯管舒"
    "屈项祝董梁杜阮蓝闵席季麻强路娄危童颜郭梅盛宋"
)


# ---------------------------------------------------------------- 风格基线


def book_profile(doc: dict) -> dict:
    lens = sorted(len(p["text"]) for ch in doc["chapters"] for p in ch["paragraphs"])
    n = max(1, len(lens))
    return {
        "n": n,
        "p10": lens[n // 10],
        "p25": lens[n // 4],
        "p50": lens[n // 2],
        "p75": lens[(3 * n) // 4],
        "p90": lens[(9 * n) // 10],
    }


# ---------------------------------------------------------------- 单段工具


def detect_punct_burst(text: str) -> tuple[bool, str]:
    """只抓真正的标点连打。

    「......」「…………」「---------」是作者的正常写法（有的作者全程这么打省略号），
    属于风格而非灌水，必须先剔除再判断，否则会大面积误报。

    这里只认「!」「！」，不认问号：盗版源里 `——` / `……` 常被整体替换成 `??`，
    把 `?` 算进来会把 746 行正常标点当成灌水（实测准确率 0%）。
    """
    stripped = SEPARATOR_RE.sub("", ELLIPSIS_RE.sub("", text))
    m = re.search(r"([!！])\1{3,}", stripped)
    if m:
        return True, f"标点连打 {m.group(0)[:6]}"
    if re.search(r"([!！]{2}){3,}", stripped):
        return True, "叹号密集"
    return False, ""


def detect_scenery(text: str, min_len: int) -> tuple[bool, str]:
    if DIALOGUE_RE.search(text) or len(text) < min_len:
        return False, ""
    hits = [w for w in SCENERY_WORDS if w in text]
    if len(hits) >= 3 and HAS_PUNCT_RE.search(text):
        return True, "写景词密集: " + "/".join(dict.fromkeys(hits))[:26]
    return False, ""


def detect_anchors(text: str, first_names: set[str]) -> tuple[list[str], float]:
    anchors: list[str] = []
    weight = 0.0
    for name in first_names:
        if name in text:
            anchors.append(f"人名首次出现: {name}")
            weight += 0.35
    for w in TURN_WORDS:
        if w in text:
            anchors.append(f"转折: {w}")
            weight += 0.3
            break
    for w in EVENT_WORDS:
        if w in text:
            anchors.append(f"事件: {w}")
            weight += 0.3
            break
    else:
        for w in WEAK_EVENT_WORDS:
            if w in text:
                anchors.append(f"弱事件: {w}")
                weight += 0.12
                break
    if TIME_JUMP_RE.search(text):
        anchors.append("时间跳跃")
        weight += 0.3
    return anchors, weight


# 名字前面出现这些字符，说明它站在句首或标点之后，是「独立成词」的位置。
# 「其余也不」「任何一」这类假名，前面的字永远是词内字，不会出现在这个集合里。
DELIM_BEFORE = set(
    " \u3000\n。，！？；：、\u201c\u201d\u2018\u2019「」『』（）()《》—…~·"
)
NAME_DELIM_RATIO = 0.35


def find_names(texts: list[str], min_count: int = 8, min_verb_hits: int = 3) -> dict[str, int]:
    """轻量人名候选：常见姓氏 + 1~2 字，出现次数达标，且在动词前出现过。

    「在动词前出现」这条是关键：中文里「严重」「任务」「任何」都符合
    「姓氏字 + 1~2 字」，但它们几乎不会紧跟「说 / 道 / 问」。

    三字候选还要过一道「前缀词频」检验：真名「张述桐」出现时，它的前缀
    「张述」词频和它相当；而「何峰一」只是「何峰」后面跟了个「一」，
    前缀词频会远高于它。小样本看不出这个区别，全书规模下必然冒出来。

    再加一道「前置定界符」检验：「余」既是姓氏也是常用字，「其余也不」
    「任何一」都会造出假名。真名前面通常是标点或句首，假名前面永远是词内字。
    """
    from collections import Counter

    counter: Counter[str] = Counter()
    verb_hits: Counter[str] = Counter()
    delim_hits: Counter[str] = Counter()
    verb_first = {v[0] for v in NAME_VERBS}
    for text in texts:
        for i, ch in enumerate(text):
            if ch not in SURNAMES:
                continue
            at_delim = i == 0 or text[i - 1] in DELIM_BEFORE
            for ln in (3, 2):
                cand = text[i : i + ln]
                if len(cand) == ln and all("\u4e00" <= c <= "\u9fff" for c in cand):
                    counter[cand] += 1
                    if at_delim:
                        delim_hits[cand] += 1
                    if text[i + ln : i + ln + 1] in verb_first:
                        verb_hits[cand] += 1

    keep: dict[str, int] = {}
    for cand, n in counter.items():
        if n < min_count or verb_hits[cand] < min_verb_hits:
            continue
        if delim_hits[cand] < 2 or delim_hits[cand] / n < NAME_DELIM_RATIO:
            continue
        if len(cand) == 3:
            prefix = counter[cand[:2]]
            if prefix and n / prefix < 0.55:
                continue
        keep[cand] = n

    # 二字候选：若已有以它开头的三字真名，则丢掉二字形式
    tri = {c for c in keep if len(c) == 3}
    return {c: n for c, n in keep.items() if not (len(c) == 2 and any(t.startswith(c) for t in tri))}


# ---------------------------------------------------------------- 作者话块


def find_note_blocks(chapters: list[dict]) -> dict[str, str]:
    """返回 pid -> 说明。作者话是整块出现的，逐段判会漏掉大半。"""
    marked: dict[str, str] = {}
    for ch in chapters:
        paras = ch["paragraphs"]
        n = len(paras)
        if n < 2:
            continue

        head = None
        for i in range(n - 1, -1, -1):
            if NOTE_HEAD_RE.match(paras[i]["text"]):
                head = i

        block: list[dict] | None = None
        reason = ""
        if head is not None:
            cand = paras[head:]
            density = sum(1 for p in cand if NOTE_KW.search(p["text"])) / len(cand)
            # 长块要求关键词密度达标，避免正文里偶发的「请假」单行把后半章误吞
            if len(cand) <= 4 or density >= NOTE_HEAD_MIN_DENSITY:
                block, reason = cand, f"显式作者话标题（密度 {density:.0%}）"
        else:
            i, gap, block_start = n - 1, 0, n
            while i >= 0:
                t = paras[i]["text"].strip()
                if NOTE_KW.search(t) or LABEL_ONLY_RE.match(t):
                    block_start, gap = i, 0
                else:
                    gap += 1
                    if gap > NOTE_MAX_GAP:
                        break
                i -= 1
            if n - block_start >= 2:
                cand = paras[block_start:]
                ratio = sum(1 for p in cand if NOTE_KW.search(p["text"])) / len(cand)
                if ratio >= NOTE_MIN_KW_RATIO:
                    block, reason = cand, f"章末关键词密集区（密度 {ratio:.0%}）"

        if not block:
            continue
        for j, p in enumerate(block):
            marked[p["pid"]] = f"{reason} · 块内第 {j + 1}/{len(block)} 段"
    return marked


# ---------------------------------------------------------------- 日常对话戏

DIALOGUE_WINDOW = 6      # 前后各看 6 段
DIALOGUE_MIN_RATIO = 0.6  # 窗口内至少这么多段含对话
DIALOGUE_MIN_SPAN = 10    # 窗口整体至少这么长，避免把两句话当一场戏


def find_dialogue_scenes(doc: dict) -> set[str]:
    """标记「以对话为主的连续段落群」。

    这是典型的「非情节信息类」：对追悬疑主线的人是水，对喜欢看角色互动的人
    是正餐。所以这里只打标签、不改分数，淡化与否交给读者偏好。
    """
    marked: set[str] = set()
    for ch in doc["chapters"]:
        paras = ch["paragraphs"]
        n = len(paras)
        if n < DIALOGUE_MIN_SPAN:
            continue
        is_dlg = [bool(DIALOGUE_RE.search(p["text"])) for p in paras]
        for i in range(n):
            s, e = max(0, i - DIALOGUE_WINDOW), min(n, i + DIALOGUE_WINDOW + 1)
            if e - s < DIALOGUE_MIN_SPAN:
                continue
            ratio = sum(is_dlg[s:e]) / (e - s)
            if ratio >= DIALOGUE_MIN_RATIO and is_dlg[i]:
                marked.add(paras[i]["pid"])
    return marked


# ---------------------------------------------------------------- 面板块

PANEL_LINE_RE = re.compile(r"^\s*[【\[](.*)[】\]]\s*$")


def is_system_line(text: str) -> bool:
    """单行系统提示：【任务开启】、【奖励：特性进化】（结算后领取）、【战场要塞】发动。

    难点是要和「正文里恰好以【】开头」区分开，比如
    「【进化游戏】唯一的危险就是一些【任务】是强制性的……」
    判据是：方括号之后几乎不能剩下什么（允许括注和很短的一个谓语）。
    """
    t = text.strip()
    if len(t) > 70 or not t or t[0] not in "【[":
        return False
    close = t.find("】" if t[0] == "【" else "]")
    if close < 2:
        return False
    inner = t[1:close].strip()
    if not inner:
        return False
    tail = t[close + 1 :]
    tail = re.sub(r"[（(][^）)]*[）)]", "", tail).strip(" \u3000。，、！？…：:")
    return len(tail) <= 6


def panel_field(text: str) -> str:
    """取面板行的字段名：【任务奖励1：100游戏币】 -> 任务奖励。"""
    m = PANEL_LINE_RE.match(text)
    inner = m.group(1) if m else text
    head = re.split(r"[：:]", inner)[0]
    head = re.sub(r"[0-9０-９]+$", "", head)
    return head.strip()


def find_panel_blocks(doc: dict) -> dict[str, tuple[str, int, int, str]]:
    """把连续的面板行聚成块，返回 pid -> (块 id, 块内序号, 块长, 字段签名)。

    面板是整块出现的（一行一个字段），逐行判定会把「【类型：言灵】」这种
    不含关键词的行漏掉，也没法做「折叠成一行」这种处置。
    """
    out: dict[str, tuple[str, int, int, str]] = {}
    bid = 0
    for ch in doc["chapters"]:
        paras = ch["paragraphs"]
        i, n = 0, len(paras)
        while i < n:
            if not PANEL_LINE_RE.match(paras[i]["text"]):
                i += 1
                continue
            j = i
            while j < n and PANEL_LINE_RE.match(paras[j]["text"]):
                j += 1
            if j - i >= 2:
                bid += 1
                sig = "/".join(dict.fromkeys(panel_field(paras[k]["text"]) for k in range(i, j)))
                for k in range(i, j):
                    out[paras[k]["pid"]] = (f"pb{bid}", k - i + 1, j - i, sig)
            i = j
    return out


# ---------------------------------------------------------------- 主流程


def analyze(doc: dict) -> dict:
    all_paras = [(ch, p) for ch in doc["chapters"] for p in ch["paragraphs"]]
    texts = [p["text"] for _, p in all_paras]
    profile = book_profile(doc)
    doc["profile"] = profile

    names = find_names(texts)
    note_blocks = find_note_blocks(doc["chapters"])
    dialogue_scenes = find_dialogue_scenes(doc)
    panel_blocks = find_panel_blocks(doc)

    # 人名首次出现的位置。一次扫完全书，避免每段回扫全部前文那种 O(n²) 写法
    # （60 万段的书上那会是几十分钟级别的开销）。
    first_seen: dict[str, int] = {}
    for _, p in all_paras:
        for n in names:
            if n not in first_seen and n in p["text"]:
                first_seen[n] = p["start"]

    # 短句连发：只有作者本来不写短段时才算信号
    short_cut = max(profile["p25"], 10)
    short_enabled = profile["p50"] >= 35
    short_run: dict[str, int] = {}
    if short_enabled:
        for ch in doc["chapters"]:
            paras = ch["paragraphs"]
            run_start = None
            for i, p in enumerate(paras):
                is_short = len(p["text"]) < short_cut
                if is_short and run_start is None:
                    run_start = i
                if not is_short and run_start is not None:
                    if i - run_start >= 3:
                        for j in range(run_start, i):
                            short_run[paras[j]["pid"]] = i - run_start
                    run_start = None
            if run_start is not None and len(paras) - run_start >= 3:
                for j in range(run_start, len(paras)):
                    short_run[paras[j]["pid"]] = len(paras) - run_start

    # 重复检测：只看前 5 章 + 本章内，避免全书 O(n²)
    repeats: dict[str, tuple[float, str]] = {}
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        vec = TfidfVectorizer(analyzer="char", ngram_range=(2, 4), min_df=2, sublinear_tf=True)
        X = vec.fit_transform(texts)

        spans, cursor = [], 0
        for ch in doc["chapters"]:
            spans.append((cursor, cursor + len(ch["paragraphs"])))
            cursor += len(ch["paragraphs"])

        for k, (s, e) in enumerate(spans):
            rows = [q for q in range(s, e) if len(texts[q]) >= 25]
            if not rows:
                continue
            prev_s = spans[max(0, k - 5)][0]
            if prev_s < s:
                sim = cosine_similarity(X[rows], X[prev_s:s])
                for r, q in enumerate(rows):
                    j = int(sim[r].argmax())
                    if sim[r][j] > 0.6:
                        repeats[all_paras[q][1]["pid"]] = (
                            float(sim[r][j]),
                            all_paras[prev_s + j][1]["pid"],
                        )
            if e - s > 1:
                sim2 = cosine_similarity(X[rows], X[s:e])
                for r, q in enumerate(rows):
                    j = int(sim2[r].argmax())
                    if s + j != q and sim2[r][j] > 0.6:
                        pid = all_paras[q][1]["pid"]
                        if pid not in repeats or sim2[r][j] > repeats[pid][0]:
                            repeats[pid] = (float(sim2[r][j]), all_paras[s + j][1]["pid"])
    except Exception as exc:  # pragma: no cover
        print(f"[warn] 重复检测跳过: {exc}")

    # 逐段判定
    for ch, p in all_paras:
        text = p["text"]
        kind, score, evidence, flags = "body", 0.55, [], []

        if p["pid"] in panel_blocks:
            block_id, seq, blen, sig = panel_blocks[p["pid"]]
            kind, score = "sys_panel", 0.15
            flags.append("panel_block")
            evidence.append(f"系统面板 {block_id} 第 {seq}/{blen} 行 · 字段 {sig[:28]}")
        elif p["pid"] in note_blocks:
            kind, score = "author_note", 0.0
            evidence.append(note_blocks[p["pid"]])
        elif NO_CONTENT_RE.match(text.strip()):
            kind, score = "separator", 0.0
            evidence.append("分隔符 / 装饰行")
        elif WATERMARK_RE.search(text):
            kind, score, evidence = "watermark", 0.0, ["平台水印 / 广告特征"]
        elif AUTHOR_NOTE_RE.search(text) and len(text) < 160:
            kind, score, evidence = "author_note", 0.0, ["章末作者话特征"]
        else:
            if is_system_line(text):
                kind, score = "sys_panel", 0.15
                evidence.append("单行系统提示")
            else:
                burst, why = detect_punct_burst(text)
                if burst and len(text) < 60:
                    kind, score, evidence = "punctuation", 0.0, [why]
                else:
                    scenery, why = detect_scenery(text, min_len=max(40, profile["p50"]))
                    if scenery:
                        flags.append("scenery")
                        evidence.append(why)
                        score -= 0.25

        if p["pid"] in short_run:
            # 只打标签，不扣分。两本真书验证下来，「段落短」是作者风格而非注水证据：
            # 《冬日重现》43% 的段落 <22 字，《序列玩家》短段也大量出现在关键叙述里。
            # 引擎不对风格下判断，要不要淡化交给读者。
            run_len = short_run[p["pid"]]
            flags.append("short_burst")
            evidence.append(f"连续短句 {run_len} 段（本书段落中位 {profile['p50']} 字）")

        if RECAP_RE.search(text):
            flags.append("recap")
            evidence.append("回顾 / 复述标记")

        # 只打标签，不扣分：这类内容的处置权完全在读者
        if p["pid"] in dialogue_scenes:
            flags.append("dialogue_scene")
            evidence.append("对话密集段群（非情节信息类，由偏好决定）")

        if p["pid"] in repeats:
            ratio, other = repeats[p["pid"]]
            flags.append("repeat")
            evidence.append(f"与 {other} 高度相似 {ratio:.0%}")
            score -= 0.35

        if kind == "body":
            first_here = {n for n in names if n in text and first_seen.get(n) == p["start"]}
            anchors, weight = detect_anchors(text, first_here)
            score += weight
            hits = [m for m in EXPOSITION_MARKERS if m in text]
            if len(hits) >= 2 and not DIALOGUE_RE.search(text):
                flags.append("exposition")
                evidence.append("旁白 / 设定解说腔: " + "/".join(hits[:4]))
                score -= 0.25
        else:
            anchors = []

        if kind == "body" and anchors and not flags:
            evidence.append("含关键锚点且无注水特征")

        p["kind"] = kind
        p["score"] = round(max(0.0, min(1.0, score)), 3)
        p["flags"] = flags
        p["anchors"] = anchors
        p["evidence"] = evidence

    doc["names"] = sorted(names)
    return doc


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: detect_rules.py <parsed.json> [-o out.json]")
        raise SystemExit(2)

    src = Path(sys.argv[1])
    out = Path(sys.argv[sys.argv.index("-o") + 1]) if "-o" in sys.argv else src.with_name("analyzed.json")

    doc = json.loads(src.read_text(encoding="utf-8"))
    analyze(doc)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
