"""AI 标定层：先找规则的系统性偏差，再只升级需要逐段裁的段落。

规则层保留所有由原文、字符特征和计数能证明的判断；这一层不重写规则，
只把规则签名聚成桶，用一小批 AI 五分类样本估计桶的标签分布，再按分歧
和预算升级分裂桶。所有产物都从 score_rule 重算，保证重复运行幂等。

用法::

    python ai_layer.py analyzed.json --endpoint URL --model NAME --key SK
                                       [--max 800] [--seed 0] [--batch 16]
                                       [--concurrency 3]
                                      [--dry]

``--dry`` 只打印抽样计划，不联网、不写文件。
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, deque
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

LABELS = ("zero", "filler", "ornament", "neutral", "plot")
LABEL_ZH = {
    "zero": "零信息",
    "filler": "注水",
    "ornament": "装饰",
    "neutral": "普通",
    "plot": "情节",
}
REP = {"zero": 0.05, "filler": 0.25, "ornament": 0.45,
       "neutral": 0.60, "plot": 0.85}
TIER_RANK = {"zero": 0, "fade": 1, "weak": 2, "mid": 3, "plot": 4}

DEFAULT_BUDGET = 800
DEFAULT_SAMPLE = 400
DEFAULT_SEED = 0
BATCH = 16
CONCURRENCY = 3
MAX_CONCURRENCY = 8
TIMEOUT = 90
MIN_BUCKET_SAMPLE = 8
RARE_BOOK = 30
POSTERIOR_M = 8.0
STABLE_P = 0.60
MAX_QUOTE_FAIL_RATE = 0.05
MAX_LABEL_SHARE = 0.95
MIN_DEGENERATE_BUCKETS = 2
MIN_BUCKET_AGREEMENT = 0.80
N_BLOCKS = 20

ENGINE = Path(__file__).resolve().parent.parent / "engine" / "webnovel-focus"

# 这五类只用于对齐尺度，不随书变化。锚点不是训练集，也不是对某本书的
# 审美判断；每类两条，给模型一个稳定的“这一类大概长什么样”的参照。
LABEL_ANCHORS = {
    "zero": (
        "求月票，求推荐票，感谢大家支持。",
        "本章未完，点击下一页继续阅读。",
    ),
    "filler": (
        "他点了点头，又摇了摇头，随后沉默了很久。",
        "她笑了笑，没有说话，屋子里还是那么安静。",
    ),
    "ornament": (
        "暮色沉下来，远处的山脊被晚霞染成暗红色。",
        "院子里的老槐树落了一地叶子，风从墙头慢慢爬过去。",
    ),
    "neutral": (
        "他推开门，走进屋里，把外套搭在椅背上。",
        "第二天一早，雨停了，街上的人渐渐多起来。",
    ),
    "plot": (
        "他把那封信撕成两半，决定从此不再回头。",
        "“其实，你父亲没有死。”她说。",
    ),
}

SYSTEM_PROMPT = """你是中文网文的段落分类编辑。规则层已经给出判定和证据，你的任务不是重写规则，也不是写文学评论，而是判断每一段属于下面五类中的哪一类，并用原文中的一句话作为依据。

五类定义（只用于对齐尺度）：
- zero：零信息。求票、水印、前情提要、整段重复；删掉不影响理解。
- filler：注水。程序化动作、无效对话、复述情绪、纯凑字；删掉基本不影响情节。
- ornament：装饰。写景、氛围、日常互动、设定说明；可能有人爱看，但不推进情节。它和 filler 必须分开。
- neutral：普通。必要的连接、过渡、信息交代，但不是转折或事件结果。
- plot：情节。人物做决定、信息揭开、关系改变、事件发生或结果。

固定锚点（只用于对齐尺度，不代表这本书）：
""" + "\n".join(
    f"- {label}：1）{LABEL_ANCHORS[label][0]} 2）{LABEL_ANCHORS[label][1]}"
    for label in LABELS
) + """

对每一段，规则层给了判定和证据；如果你不同意，请指出是哪一条证据不对，并引用原文。不要因为规则分数低就机械判低，也不要因为规则分数高就机械判高。

只输出一个 JSON 数组，每项严格为：
{"i":序号,"label":"zero|filler|ornament|neutral|plot","quote":"原文中的一句","why":"不超过40字","rule_error":"none|over|under","rule_note":"哪条规则证据不对；没有就写 none"}

i 必须与输入逐项对应，顺序一致，不能遗漏、重复或改写。quote 必须逐字出现在该段的 text、prev 或 next 中。"""

class ReviewError(RuntimeError):
    """标定不能产出可信结论时抛出的显式错误。"""


def _score_text(score: object) -> str:
    try:
        value = float(score)
    except (TypeError, ValueError):
        value = 0.55
    text = f"{max(0.0, min(1.0, value)):.3f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += ".00"
    elif len(text.split(".", 1)[1]) < 2:
        text += "0"
    return text


def _flags_text(flags: object) -> str:
    if not isinstance(flags, (list, tuple, set)):
        return ""
    return "+".join(sorted({str(flag) for flag in flags if str(flag)}))


def signature(score: object, flags: object) -> str:
    return f"body|{_score_text(score)}|{_flags_text(flags)}"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _label_tier(label: str) -> str:
    return {
        "zero": "zero", "filler": "fade", "ornament": "weak",
        "neutral": "mid", "plot": "plot",
    }.get(label, "mid")


def _tier_of(kind: str, score: object) -> str:
    """调用引擎自己的 tiers.tier_of；引擎不可用时才退回同一份映射。"""
    if kind == "separator":
        return "sep"
    if kind in {"author_note", "watermark", "punctuation"}:
        return "zero"
    scripts = ENGINE / "scripts"
    if scripts.is_dir() and str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    try:
        from tiers import tier_of  # noqa: PLC0415
        return tier_of(kind, _number(score, 0.0))
    except Exception:
        value = _number(score, 0.0)
        if value < 0.12:
            return "zero"
        if value < 0.38:
            return "fade"
        if value < 0.52:
            return "weak"
        if value < 0.68:
            return "mid"
        return "plot"


def _rule_error(label: str, rule_score: object) -> str:
    """AI 类别相对规则分档是判高了还是判低了。"""
    ai = _label_tier(label)
    rule = _tier_of("body", rule_score)
    a, r = TIER_RANK.get(ai, 3), TIER_RANK.get(rule, 3)
    if a < r:
        return "over"
    if a > r:
        return "under"
    return "none"


def _clean_label(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in LABELS else None


def _write_progress(**kw) -> None:
    """服务端轮询用的 progress.json；路径来自 WNF_PROGRESS。"""
    path = os.environ.get("WNF_PROGRESS", "")
    if not path:
        return
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(kw, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _ensure_score_rule(doc: dict) -> None:
    """把规则分固定下来；已经存在的 score_rule 永不覆盖。"""
    for chapter in doc.get("chapters", []):
        for para in chapter.get("paragraphs", []):
            if "score_rule" not in para:
                para["score_rule"] = round(_number(para.get("score"), 0.55), 3)


def _chapter_title(chapter: dict, index: int) -> str:
    return str(chapter.get("title") or f"第 {index + 1} 章")


def _is_kept(para: dict) -> bool:
    """读者已经保留的段落不能由自动层改分。"""
    return any(bool(para.get(key)) for key in
               ("keep", "kept", "manual_keep", "preserve", "reserved"))


def _body_refs(doc: dict) -> list[dict]:
    """返回按 pid 排序的 body 段引用，附带上下文和规则签名。"""
    refs: list[dict] = []
    for ci, chapter in enumerate(doc.get("chapters", [])):
        paras = chapter.get("paragraphs", [])
        for pi, para in enumerate(paras):
            if para.get("kind") != "body":
                continue
            if _is_kept(para):
                continue
            score = para.get("score_rule", para.get("score", 0.55))
            flags = list(para.get("flags") or [])
            rule_evidence = [
                str(item) for item in (para.get("evidence") or [])
                if not str(item).startswith(("标定:", "AI 复核"))
            ]
            prev_text = paras[pi - 1].get("text", "") if pi > 0 else ""
            next_text = paras[pi + 1].get("text", "") if pi + 1 < len(paras) else ""
            refs.append({
                "pid": str(para.get("pid") or f"c{ci + 1}p{pi + 1}"),
                "ci": ci,
                "pi": pi,
                "text": str(para.get("text") or ""),
                "prev": str(prev_text or ""),
                "next": str(next_text or ""),
                "chapter": _chapter_title(chapter, ci),
                "score": _number(score, 0.55),
                "flags": flags,
                "anchors": list(para.get("anchors") or []),
                "evidence": rule_evidence,
                "sig": signature(score, flags),
            })
    refs.sort(key=lambda ref: ref["pid"])
    return refs


def population_hash(refs: list[dict]) -> str:
    """总体哈希只覆盖会影响抽样的正文记录。"""
    records = [
        {"pid": r["pid"], "score": r["score"], "flags": r["flags"], "text": r["text"]}
        for r in sorted(refs, key=lambda item: item["pid"])
    ]
    return _hash(json.dumps(records, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")))


def _largest_remainder(total: int, weights: list[float], minimum: int = 0) -> list[int]:
    """按权重分配整数配额，余数用最大余数法，结果确定。"""
    if total <= 0 or not weights:
        return [0] * len(weights)
    alive = [i for i, weight in enumerate(weights) if weight > 0]
    out = [0] * len(weights)
    if not alive:
        return out
    base = min(minimum, max(0, total // len(alive)))
    for i in alive:
        out[i] = base
    remaining = total - base * len(alive)
    if remaining <= 0:
        return out
    total_w = sum(weights[i] for i in alive)
    exact = {i: remaining * weights[i] / total_w for i in alive}
    for i in alive:
        out[i] += int(math.floor(exact[i]))
    left = total - sum(out)
    order = sorted(alive, key=lambda i: (-(exact[i] - math.floor(exact[i])), -weights[i], i))
    for i in order[:left]:
        out[i] += 1
    return out


def _bucket_targets(pool_sizes: dict[str, int], budget: int) -> dict[str, int]:
    """按 sqrt(n) 给桶分配样本目标；超出或不足时保持总预算可解释。"""
    keys = sorted(pool_sizes)
    if not keys or budget <= 0:
        return {key: 0 for key in keys}
    root_sum = sum(math.sqrt(max(0, pool_sizes[key])) for key in keys)
    if root_sum <= 0:
        return {key: 0 for key in keys}
    raw = {
        key: max(MIN_BUCKET_SAMPLE,
                 int(round(budget * math.sqrt(max(0, pool_sizes[key])) / root_sum)))
        for key in keys
    }
    if sum(raw.values()) <= budget:
        return raw
    # 最少样本与硬预算冲突时，用配额分配收口。正常书通常不会走到这里；
    # 一旦走到，报告会通过 target 与 picked 的差额显示出来。
    minimum = MIN_BUCKET_SAMPLE if budget >= MIN_BUCKET_SAMPLE * len(keys) else 1
    allocated = _largest_remainder(budget, [float(pool_sizes[key]) for key in keys],
                                   minimum=minimum)
    return {key: allocated[i] for i, key in enumerate(keys)}


def _chapter_blocks(doc: dict, refs: list[dict]) -> list[dict]:
    chapters = doc.get("chapters", [])
    n = len(chapters)
    if n == 0:
        return []
    blocks: list[dict] = []
    if n <= N_BLOCKS:
        ranges = [(i, i + 1) for i in range(n)]
    else:
        ranges = []
        for b in range(N_BLOCKS):
            start = (b * n) // N_BLOCKS
            end = ((b + 1) * n) // N_BLOCKS
            if end <= start:
                end = min(n, start + 1)
            ranges.append((start, end))
    for bi, (start, end) in enumerate(ranges):
        blocks.append({
            "index": bi,
            "chapter_start": start,
            "chapter_end": end,
            "chapters": end - start,
            "n_body": sum(1 for ref in refs if start <= ref["ci"] < end),
            "quota": 0,
            "n_sample": 0,
        })
    return blocks


def _pick_from_block(queue: deque, selected: set[str],
                     chapter_counts: dict[int, int],
                     chapter_limit: int = 3) -> dict | None:
    """从块内一个桶的队列取一段，遵守当前阶段的每章上限。"""
    if not queue:
        return None
    for _ in range(len(queue)):
        ref = queue.popleft()
        if ref["pid"] in selected:
            continue
        if chapter_counts.get(ref["ci"], 0) >= chapter_limit:
            queue.append(ref)
            continue
        return ref
    return None


def plan_sample(doc: dict, budget: int = DEFAULT_BUDGET,
                seed: int = DEFAULT_SEED, sample_cap: int = DEFAULT_SAMPLE) -> dict:
    """确定性分层抽样计划；只读 doc，不联网、不写文件。"""
    _ensure_score_rule(doc)
    refs = _body_refs(doc)
    if not refs:
        return {
            "seed": seed, "population_hash": population_hash(refs),
            "n_paras": 0, "n_sample": 0, "sample_pids": [], "items": [],
            "refs": [], "buckets": [], "blocks": [], "chars": 0,
            "target": 0, "shortfall": 0, "chapter_limit": 3,
        }

    n_target = min(max(0, int(sample_cap)), max(0, int(budget)), len(refs))
    n_chapters = max(1, len(doc.get("chapters", [])))
    chapter_limit = max(3, math.ceil(n_target / n_chapters))
    exact: dict[str, list[dict]] = {}
    for ref in refs:
        exact.setdefault(ref["sig"], []).append(ref)
    sample_pool: dict[str, str] = {}
    pool_sizes: dict[str, int] = {}
    for key, items in exact.items():
        pool = key if len(items) >= MIN_BUCKET_SAMPLE else "other"
        sample_pool[key] = pool
        pool_sizes[pool] = pool_sizes.get(pool, 0) + len(items)
    targets = _bucket_targets(pool_sizes, n_target)

    blocks = _chapter_blocks(doc, refs)
    block_sizes = [block["n_body"] for block in blocks]
    block_quotas = _largest_remainder(
        n_target, [float(size) for size in block_sizes],
        minimum=1 if n_target >= sum(1 for size in block_sizes if size) else 0,
    )
    for block, quota in zip(blocks, block_quotas):
        block["quota"] = quota

    # 每个 (抽样桶, 章节块) 一个队列；队列顺序由固定 seed 决定。
    rng = random.Random(seed)
    queues: dict[tuple[str, int], deque] = {}
    block_of_ci = {}
    for block in blocks:
        for ci in range(block["chapter_start"], block["chapter_end"]):
            block_of_ci[ci] = block["index"]
    queue_items: dict[tuple[str, int], list[dict]] = {}
    for ref in refs:
        block_index = block_of_ci.get(ref["ci"])
        if block_index is None:
            continue
        key = (sample_pool[ref["sig"]], block_index)
        queue_items.setdefault(key, []).append(ref)
    for key, items in queue_items.items():
        items.sort(key=lambda ref: ref["pid"])
        rng.shuffle(items)
        queues[key] = deque(items)
    # 空队列也要存在，避免 take() 直接 KeyError。
    for pool in pool_sizes:
        for block in blocks:
            queues.setdefault((pool, block["index"]), deque())

    selected: set[str] = set()
    selected_refs: list[dict] = []
    chapter_counts: dict[int, int] = {}
    remaining_target = dict(targets)

    def take(pool: str, block_index: int) -> dict | None:
        if remaining_target.get(pool, 0) <= 0:
            return None
        ref = _pick_from_block(queues[(pool, block_index)], selected,
                               chapter_counts, chapter_limit)
        if ref is None:
            return None
        selected.add(ref["pid"])
        chapter_counts[ref["ci"]] = chapter_counts.get(ref["ci"], 0) + 1
        remaining_target[pool] -= 1
        item = dict(ref)
        item["block"] = block_index
        item["sample_bucket"] = pool
        selected_refs.append(item)
        blocks[block_index]["n_sample"] += 1
        return item

    # 第一轮：每块按桶轮流，把章节覆盖做出来。
    for block in blocks:
        while block["n_sample"] < block["quota"]:
            moved = False
            for pool in sorted(remaining_target):
                if block["n_sample"] >= block["quota"]:
                    break
                if take(pool, block["index"]) is not None:
                    moved = True
            if not moved:
                break

    # 第二轮：块配额因章节上限没填满时，在还有容量的块里补齐。
    if len(selected_refs) < n_target:
        for block in blocks:
            for pool in sorted(remaining_target):
                if len(selected_refs) >= n_target:
                    break
                take(pool, block["index"])
            if len(selected_refs) >= n_target:
                break

    selected_refs.sort(key=lambda ref: ref["pid"])
    bucket_rows = []
    for key in sorted(exact):
        pool = sample_pool[key]
        bucket_rows.append({
            "key": key,
            "n_book": len(exact[key]),
            "sample_pool": pool,
            "target": targets.get(pool, 0) if pool == key else 0,
            "n_sample": sum(1 for item in selected_refs if item["sig"] == key),
        })
    if "other" in targets:
        bucket_rows.append({
            "key": "other",
            "n_book": pool_sizes["other"],
            "sample_pool": "other",
            "target": targets["other"],
            "n_sample": sum(1 for item in selected_refs if item["sample_bucket"] == "other"),
        })
    chars = sum(len(item["text"]) for item in selected_refs)
    return {
        "seed": seed,
        "chapter_limit": chapter_limit,
        "population_hash": population_hash(refs),
        "n_paras": len(refs),
        "n_sample": len(selected_refs),
        "target": n_target,
        "shortfall": max(0, n_target - len(selected_refs)),
        "chars": chars,
        "sample_pids": [item["pid"] for item in selected_refs],
        "items": selected_refs,
        "refs": refs,
        "buckets": bucket_rows,
        "blocks": blocks,
    }


def format_sample_plan(plan: dict) -> str:
    lines = [
        f"抽样计划：正文 {plan['n_paras']} 段，目标 {plan['target']} 段，"
        f"实际 {plan['n_sample']} 段，{plan['chars']} 字，seed={plan['seed']}，"
        f"每章上限 {plan.get('chapter_limit', 3)}",
    ]
    lines.append("桶：")
    for row in plan["buckets"]:
        picked = row.get("n_sample", row.get("n_samples", 0))
        lines.append(f"  {row['key']}  n_book={row['n_book']}  "
                     f"target={row['target']}  picked={picked}")
    lines.append("章节块：")
    for block in plan["blocks"]:
        lines.append(f"  块{block['index'] + 1:02d} "
                     f"章{block['chapter_start'] + 1}-{block['chapter_end']} "
                     f"正文{block['n_body']} 配额{block['quota']} "
                     f"抽{block['n_sample']}")
    return "\n".join(lines)


def _request_item(ref: dict, i: int) -> dict:
    return {
        "i": i,
        "text": ref["text"],
        "prev": ref["prev"],
        "next": ref["next"],
        "chapter": ref["chapter"],
        "rule": {
            "score": round(ref["score"], 3),
            "flags": ref["flags"],
            "anchors": ref["anchors"],
            "evidence": ref["evidence"],
        },
    }


def _extract_array(content: str) -> list:
    match = re.search(r"\[[\s\S]*\]", content or "")
    if not match:
        raise ValueError("模型没有返回 JSON 数组")
    value = json.loads(match.group(0))
    if not isinstance(value, list):
        raise ValueError("模型返回的不是 JSON 数组")
    return value


def _quote_bad(item: dict, returned: dict) -> bool:
    quote = str(returned.get("quote") or "").strip()
    return (not quote) or not any(
        quote in str(item.get(key) or "") for key in ("text", "prev", "next"))


def validate_reply(items: list[dict], raw: object) -> tuple[list[dict] | None, str, int]:
    """校验 AI 返回；返回 (规范化结果, 错误, 最终引用坏条数)。

    标签、顺序和条目数属于硬契约；quote 属于证据质量。单条 quote 不合法时保留
    已经通过的 label，先把 quote_bad 计数交给上层做少量重试和总体比例闸门。
    """
    if not isinstance(raw, list):
        return None, "返回不是数组", 0
    if len(raw) != len(items):
        quote_bad = sum(
            1 for item, returned in zip(items, raw)
            if isinstance(returned, dict) and _quote_bad(item, returned)
        )
        return None, f"返回 {len(raw)} 项，请求 {len(items)} 项", quote_bad
    out: list[dict] = []
    errors: list[str] = []
    quote_bad = 0
    for expected_i, item in enumerate(items):
        returned = raw[expected_i]
        if not isinstance(returned, dict):
            errors.append(f"第 {expected_i + 1} 项不是对象")
            continue
        valid_item = True
        try:
            got_i = int(returned.get("i"))
        except (TypeError, ValueError):
            got_i = None
        if got_i != expected_i:
            valid_item = False
            errors.append(f"第 {expected_i + 1} 项 i 顺序或编号不对")
        label = _clean_label(returned.get("label"))
        if label is None:
            valid_item = False
            errors.append(f"第 {expected_i + 1} 项 label 不在五类枚举内")
        bad_quote = _quote_bad(item, returned)
        if bad_quote:
            quote_bad += 1
        if not valid_item or label is None:
            continue
        score = item.get("rule", {}).get("score", 0.55)
        claim = str(returned.get("rule_error") or "").strip().lower()
        if claim not in {"none", "over", "under"}:
            claim = None
        computed = _rule_error(label, score)
        out.append({
            "i": expected_i,
            "label": label,
            "quote": str(returned.get("quote") or "").strip(),
            "quote_ok": not bad_quote,
            "why": str(returned.get("why") or "").strip()[:120],
            "rule_error": computed,
            "model_claim": claim,
            "rule_error_match": claim == computed if claim else False,
            "rule_note": str(returned.get("rule_note") or "").strip()[:160] or "none",
        })
    if errors:
        return None, "；".join(errors[:3]), quote_bad
    return out, "", quote_bad


def ask(items: list[dict], endpoint: str, model: str, key: str,
        timeout: int = TIMEOUT, error_hint: str = "",
        system_prompt: str = SYSTEM_PROMPT) -> list:
    """发一批五分类请求；只负责网络和 JSON 提取。"""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
    ]
    if error_hint:
        messages.append({
            "role": "user",
            "content": f"上一次返回未通过校验：{error_hint}。请重新返回严格符合约定的 JSON 数组。",
        })
    payload = {
        "model": model,
        "temperature": 0,
        "messages": messages,
    }
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json",
                 "authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8", "replace"))
    content = (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    return _extract_array(content)


def _call_with_network_retries(items: list[dict], endpoint: str, model: str,
                               key: str, error_hint: str = "",
                               timeout: int = TIMEOUT,
                               system_prompt: str = SYSTEM_PROMPT) -> tuple[list | None, str]:
    last = ""
    for attempt in range(3):
        try:
            return ask(items, endpoint, model, key, timeout=timeout,
                       error_hint=error_hint, system_prompt=system_prompt), ""
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError) as exc:
            last = str(exc)
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    return None, last


def ask_batch(items: list[dict], endpoint: str, model: str, key: str
              ) -> tuple[list[dict] | None, str, dict]:
    """一次请求 + 一次契约重试；网络重试沿用 3 次节奏。

    标签/编号/条目数不合规才让整批失败。仅 quote 不合规时尝试重问一次，
    仍不合规就保留标签并记录 quote_bad，最终由全书 quote 失败率闸门决定。
    """
    stats = {
        "quote_bad": 0,
        "first_pass_items": 0,
        "first_pass_batches": 0,
        "contract_retries": 0,
    }
    raw, err = _call_with_network_retries(items, endpoint, model, key)
    if raw is None:
        return None, err or "网络请求失败", stats
    valid, verr, quote_bad = validate_reply(items, raw)
    stats["quote_bad"] = quote_bad
    if valid is not None:
        if quote_bad == 0:
            stats["first_pass_items"] = len(items)
            stats["first_pass_batches"] = 1
            return valid, "", stats
        # 引用证据不可靠，但 labels 都合法：尝试单独把证据修一次。
        stats["contract_retries"] = 1
        raw2, err2 = _call_with_network_retries(
            items, endpoint, model, key,
            error_hint=f"有 {quote_bad} 条 quote 不是原文子串，请逐字复制原文。",
        )
        if raw2 is not None:
            valid2, _, quote_bad2 = validate_reply(items, raw2)
            if valid2 is not None:
                stats["quote_bad"] = quote_bad2
                return valid2, "", stats
        # 第二次仍只坏 quote：保留第一次已经通过硬契约的 labels。
        return valid, "", stats
    stats["contract_retries"] = 1
    raw, err = _call_with_network_retries(items, endpoint, model, key, error_hint=verr)
    if raw is None:
        stats["quote_bad"] = quote_bad
        return None, err or "契约重试失败", stats
    valid, verr, quote_bad2 = validate_reply(items, raw)
    stats["quote_bad"] = quote_bad2
    if valid is None:
        return None, verr, stats
    return valid, "", stats


def _ask_refs(refs: list[dict], endpoint: str, model: str, key: str,
              batch: int, stage: str,
              concurrency: int = CONCURRENCY) -> tuple[list[dict], dict]:
    """按固定批次问 AI；批次可并发，但结果始终按原批次顺序合并。"""
    batch = max(1, int(batch))
    labels: list[dict] = []
    failed = 0
    quote_failures = 0
    first_pass_items = 0
    first_pass_batches = 0
    contract_retries = 0
    asked = 0
    total = (len(refs) + batch - 1) // batch if refs else 0
    if total == 0:
        _write_progress(stage=stage, done=0, total=0, reviewed=0, failed=0)
        return labels, {"asked": 0, "chars": 0, "batches": 0, "failed": 0,
                        "quote_failures": 0, "first_pass_items": 0,
                        "first_pass_batches": 0, "contract_retries": 0,
                        "reviewed": 0}
    chunks = [refs[start:start + batch] for start in range(0, len(refs), batch)]

    def ask_chunk(index: int, chunk: list[dict]):
        request = [_request_item(ref, i) for i, ref in enumerate(chunk)]
        valid, err, batch_stats = ask_batch(request, endpoint, model, key)
        return index, chunk, valid, err, batch_stats

    workers = max(1, min(int(concurrency), MAX_CONCURRENCY, total))
    labels_by_batch: list[list[dict] | None] = [None] * total
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(ask_chunk, index, chunk): index
            for index, chunk in enumerate(chunks)
        }
        for future in as_completed(futures):
            index = futures[future]
            chunk = chunks[index]
            try:
                _, chunk, valid, err, batch_stats = future.result()
            except Exception as exc:  # noqa: BLE001
                valid, err, batch_stats = None, str(exc), {}
            done += 1
            asked += len(chunk)
            quote_failures += batch_stats.get("quote_bad", 0)
            first_pass_items += batch_stats.get("first_pass_items", 0)
            first_pass_batches += batch_stats.get("first_pass_batches", 0)
            contract_retries += batch_stats.get("contract_retries", 0)
            if valid is None:
                failed += 1
                print(f"[warn] {stage} 第 {index + 1} 批失败：{err}", file=sys.stderr)
            else:
                labels_by_batch[index] = [
                    {**item, "pid": ref["pid"], "ref": ref}
                    for ref, item in zip(chunk, valid)
                ]
            reviewed = sum(len(items or []) for items in labels_by_batch)
            _write_progress(stage=stage, done=done, total=total,
                            reviewed=reviewed, failed=failed)

    labels = [item for items in labels_by_batch for item in (items or [])]
    return labels, {
        "asked": asked,
        "chars": sum(len(ref["text"]) for ref in refs),
        "batches": total,
        "failed": failed,
        "quote_failures": quote_failures,
        "first_pass_items": first_pass_items,
        "first_pass_batches": first_pass_batches,
        "contract_retries": contract_retries,
        "reviewed": len(labels),
    }


def _p_global(labels: list[dict]) -> dict[str, float]:
    counts = Counter(item["label"] for item in labels if item.get("label") in LABELS)
    total = sum(counts.values())
    if total <= 0:
        return {label: 1.0 / len(LABELS) for label in LABELS}
    return {label: counts.get(label, 0) / total for label in LABELS}


def fit_buckets(sample_plan: dict, labels: list[dict]) -> tuple[dict[str, dict], dict]:
    """Dirichlet 后验均值 -> 每个精确规则签名的桶。"""
    prior = _p_global(labels)
    labels_by_pid = {item["pid"]: item["label"] for item in labels}
    exact: dict[str, list[dict]] = {}
    for ref in sample_plan["refs"]:
        exact.setdefault(ref["sig"], []).append(ref)
    buckets: dict[str, dict] = {}
    for key in sorted(exact):
        n_book = len(exact[key])
        counts = Counter()
        for ref in exact[key]:
            label = labels_by_pid.get(ref["pid"])
            if label in LABELS:
                counts[label] += 1
        n_sample = sum(counts.values())
        p = {
            label: (counts.get(label, 0) + POSTERIOR_M * prior[label])
            / (n_sample + POSTERIOR_M)
            for label in LABELS
        }
        entropy = -sum(value * math.log(value, 2) for value in p.values() if value > 0)
        e_value = sum(p[label] * REP[label] for label in LABELS)
        max_label = max(LABELS, key=lambda label: p[label])
        rare = n_book <= RARE_BOOK
        stable = (not rare and n_sample > 0 and max(p.values()) >= STABLE_P)
        buckets[key] = {
            "key": key,
            "n_book": n_book,
            "n_sample": n_sample,
            "labels": {label: counts.get(label, 0) for label in LABELS},
            "p": {label: round(p[label], 6) for label in LABELS},
            "E": round(e_value, 6),
            "tier_cal": _tier_of("body", e_value),
            "entropy": round(entropy, 6),
            "gain": round(n_book * entropy, 6),
            "max_label": max_label,
            "stable": stable,
            "rare": rare,
            "escalated": 0,
            "pending": not stable,
        }
    return buckets, prior


def _bucket_gain(bucket: dict) -> float:
    return bucket["n_book"] * bucket["entropy"]


def _label_guard(labels: list[dict], buckets: dict[str, dict]) -> dict:
    """合并样本的单类硬门 + 桶间主标签一致率，避免单个集中桶误杀全书。"""
    counts = Counter(item["label"] for item in labels if item.get("label") in LABELS)
    total = sum(counts.values())
    if total <= 0:
        return {
            "dominant_label": None, "max_share": 0.0,
            "eligible_buckets": 0, "bucket_agreement": 0.0,
            "degenerate": False, "warning": False,
        }
    dominant, count = counts.most_common(1)[0]
    eligible = [bucket for bucket in buckets.values() if bucket.get("n_sample", 0) >= 8]
    agree = sum(1 for bucket in eligible if bucket.get("max_label") == dominant)
    agreement = (agree / len(eligible)) if eligible else 0.0
    share = count / total
    degenerate = (
        share > MAX_LABEL_SHARE
        and len(eligible) >= MIN_DEGENERATE_BUCKETS
        and agreement >= MIN_BUCKET_AGREEMENT
    )
    return {
        "dominant_label": dominant,
        "max_share": round(share, 6),
        "eligible_buckets": len(eligible),
        "bucket_agreement": round(agreement, 6),
        "degenerate": degenerate,
        "warning": share > MAX_LABEL_SHARE and not degenerate,
    }


def plan_escalation(sample_plan: dict, buckets: dict[str, dict],
                    budget: int, seed: int) -> tuple[list[dict], dict[str, int]]:
    """先升级稀有桶，再按 n_book × 熵 升级分裂桶。"""
    sampled = set(sample_plan["sample_pids"])
    candidates_by_key: dict[str, list[dict]] = {}
    for ref in sample_plan["refs"]:
        if ref["pid"] in sampled:
            continue
        candidates_by_key.setdefault(ref["sig"], []).append(ref)
    rare_keys = [key for key, bucket in buckets.items() if bucket["rare"]]
    split_keys = [key for key, bucket in buckets.items()
                  if not bucket["rare"] and not bucket["stable"]]
    rare_keys.sort(key=lambda key: (-buckets[key]["n_book"], key))
    split_keys.sort(key=lambda key: (-_bucket_gain(buckets[key]), key))
    rng = random.Random(seed + 1000003)
    selected: list[dict] = []
    taken: set[str] = set()
    chapter_counts: dict[int, int] = {}
    n_chapters = max(1, len({ref["ci"] for ref in sample_plan.get("refs", [])}))
    chapter_limit = max(3, math.ceil(max(1, int(budget)) / n_chapters))
    per_bucket: Counter = Counter()
    remaining = max(0, int(budget))
    for key in rare_keys + split_keys:
        if remaining <= 0:
            break
        items = sorted(candidates_by_key.get(key, []), key=lambda ref: ref["pid"])
        rng.shuffle(items)
        queue = deque(items)
        while remaining > 0 and queue:
            ref = _pick_from_block(queue, taken, chapter_counts, chapter_limit)
            if ref is None:
                break
            taken.add(ref["pid"])
            chapter_counts[ref["ci"]] = chapter_counts.get(ref["ci"], 0) + 1
            item = dict(ref)
            item["escalation_bucket"] = key
            selected.append(item)
            per_bucket[key] += 1
            remaining -= 1
    selected.sort(key=lambda ref: ref["pid"])
    return selected, dict(per_bucket)


def _diagnose_labels(sample_labels: list[dict], escalation_labels: list[dict],
                     refs_by_pid: dict[str, dict]) -> tuple[list[dict], dict, dict]:
    """从所有个体标签生成分歧清单和按签名的 rule_error 汇总。"""
    disagreements: list[dict] = []
    by_bucket: dict[str, dict[str, int]] = {}
    totals = {"none": 0, "over": 0, "under": 0}
    claim_mismatches = 0
    all_labels = sample_labels + escalation_labels
    for item in all_labels:
        ref = item.get("ref") or refs_by_pid.get(item.get("pid", ""))
        if ref is None:
            continue
        label = item.get("label")
        if label not in LABELS:
            continue
        err = _rule_error(label, ref["score"])
        claim = item.get("model_claim")
        if claim and claim != err:
            claim_mismatches += 1
        totals[err] = totals.get(err, 0) + 1
        bucket = by_bucket.setdefault(ref["sig"], {"none": 0, "over": 0, "under": 0})
        bucket[err] += 1
        if _label_tier(label) != _tier_of("body", ref["score"]):
            disagreements.append({
                "pid": ref["pid"],
                "bucket": ref["sig"],
                "rule_score": round(ref["score"], 3),
                "rule_tier": _tier_of("body", ref["score"]),
                "rule_evidence": list(ref["evidence"]),
                "ai_label": label,
                "quote": item.get("quote", ""),
                "why": item.get("why", ""),
                "rule_error": err,
                "model_claim": claim,
                "rule_error_match": bool(claim == err) if claim else False,
                "rule_note": item.get("rule_note", "none"),
            })
    disagreements.sort(key=lambda item: item["pid"])
    return (disagreements[:50],
            {"totals": totals, "by_bucket": by_bucket,
             "claim_mismatches": claim_mismatches},
            by_bucket)


def _cal_evidence(sig: str, bucket: dict, pending: bool) -> str:
    if pending and bucket["rare"]:
        return f"标定: {sig} → 待升级 (n_book {bucket['n_book']}, 稀有桶)"
    label = bucket["max_label"]
    pct = bucket["p"].get(label, 0.0)
    return (f"标定: {sig} → {bucket['tier_cal']} "
            f"(样本 {bucket['n_sample']}，AI {LABEL_ZH[label]} {pct:.0%})")


def _append_evidence(para: dict, line: str) -> None:
    evidence = [str(item) for item in (para.get("evidence") or [])
                if not str(item).startswith("标定:")]
    evidence.append(line)
    para["evidence"] = evidence


def apply_calibration(doc: dict, sample_plan: dict, buckets: dict[str, dict],
                      escalation_labels: list[dict]) -> dict:
    """把桶结果或个体升级标签写回 analyzed.json。"""
    individual = {item["pid"]: item for item in escalation_labels
                  if item.get("label") in LABELS}
    refs_by_pid = {ref["pid"]: ref for ref in sample_plan["refs"]}
    before = Counter()
    after = Counter()
    pending_by_bucket: Counter = Counter()
    reviewed = 0
    for chapter in doc.get("chapters", []):
        for para in chapter.get("paragraphs", []):
            score_rule = _number(para.get("score_rule", para.get("score", 0.55)), 0.55)
            before[_tier_of(para.get("kind", "body"), score_rule)] += 1
            if para.get("kind") != "body":
                if "score_rule" not in para:
                    para["score_rule"] = round(score_rule, 3)
                after[_tier_of(para.get("kind", "body"),
                               _number(para.get("score"), 0.0))] += 1
                continue
            pid = str(para.get("pid") or "")
            ref = refs_by_pid.get(pid)
            if ref is None:
                after[_tier_of("body", _number(para.get("score"), 0.0))] += 1
                continue
            bucket = buckets.get(ref["sig"])
            if bucket is None:
                after[_tier_of("body", _number(para.get("score"), 0.0))] += 1
                continue
            item = individual.get(pid)
            if item is not None:
                score = REP[item["label"]]
                source = "escalated"
                pending = False
            elif score_rule >= 0.68 and bucket["max_label"] == "plot":
                # 规则层给出的高置信情节锚点不能被一个不稳定的桶平均分抹平。
                # AI 仍可压低普通/模糊段，但这里保留“关键情节永远保护”的底线。
                score = max(score_rule, float(bucket["E"]))
                source = "rule_guard"
                pending = False
            elif bucket["stable"]:
                score = bucket["E"]
                source = "bucket"
                pending = False
            elif bucket["rare"]:
                score = score_rule
                source = "rule"
                pending = True
            else:
                score = bucket["E"]
                source = "bucket"
                pending = True
            para["score"] = round(max(0.0, min(1.0, score)), 3)
            para["cal"] = {
                "bucket": ref["sig"],
                "n_sample": bucket["n_sample"],
                "source": source,
                "p": dict(bucket["p"]),
                "pending": pending,
            }
            if pending:
                pending_by_bucket[ref["sig"]] += 1
            if source == "rule_guard":
                _append_evidence(
                    para,
                    f"标定: {ref['sig']} → 情节保护 "
                    f"(规则分 {score_rule:.2f}，AI 桶倾向情节)",
                )
            else:
                _append_evidence(para, _cal_evidence(ref["sig"], bucket, pending))
            after[_tier_of("body", para["score"])] += 1
            reviewed += 1
    # 非 body 也补 score_rule；它们不参与标定，但下一次运行要从规则分算。
    for chapter in doc.get("chapters", []):
        for para in chapter.get("paragraphs", []):
            if "score_rule" not in para:
                para["score_rule"] = round(_number(para.get("score"), 0.0), 3)
    return {"reviewed": reviewed, "tier_before": dict(before),
            "tier_after": dict(after),
            "pending_by_bucket": dict(pending_by_bucket)}


def _endpoint_host(endpoint: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(endpoint or "")
        return parsed.hostname or ""
    except ValueError:
        return ""


def _calibration_path(analyzed: Path) -> Path:
    if analyzed.parent.name == "out":
        return analyzed.parent.parent / "calibration.json"
    return analyzed.parent / "calibration.json"


def _report_path(analyzed: Path) -> Path:
    return _calibration_path(analyzed).with_name("calibration_report.txt")


def _bucket_table_text(buckets: dict[str, dict]) -> str:
    lines = ["key\tn_book\tn_sample\tlabels\tE\ttier_cal\tentropy"]
    for key in sorted(buckets):
        b = buckets[key]
        dis = ",".join(f"{label}:{n}" for label, n in b["labels"].items() if n)
        lines.append(f"{key}\t{b['n_book']}\t{b['n_sample']}\t{dis or '-'}\t"
                     f"{b['E']:.3f}\t{b['tier_cal']}\t{b['entropy']:.3f}")
    return "\n".join(lines)


def _human_report(cal: dict, buckets: dict[str, dict]) -> str:
    metrics = cal.get("metrics", {})
    stable = sum(1 for bucket in buckets.values() if bucket["stable"])
    split = sum(1 for bucket in buckets.values() if not bucket["stable"])
    rare = sum(1 for bucket in buckets.values() if bucket["rare"])
    first_pass = metrics.get("first_pass_rate")
    first_pass_text = (f"首次通过率：{first_pass:.1%}" if isinstance(first_pass, (int, float))
                       else "首次通过率：未记录")
    lines = [
        f"标定报告 v{cal.get('version')}",
        f"样本：{cal.get('n_sample')} / 正文 {cal.get('n_paras')} 段；"
        f"升级：{cal.get('n_escalated')} 段",
        f"AI 个别裁定覆盖：{metrics.get('individual_coverage', 0.0):.1%}；"
        f"桶级标定覆盖：{metrics.get('bucket_coverage', 0.0):.1%}",
        f"桶：{len(buckets)} 个（稳定 {stable} / 分裂 {split} / 稀有 {rare}）；"
        f"待升级：{metrics.get('escalation_pending', 0)} 段；"
        f"规则无信号：{metrics.get('no_signal', 0)} 段",
        (f"引用校验失败率：{metrics.get('quote_failure_rate', 0.0):.1%}；"
         + first_pass_text + "；"
         + f"抽样 shortfall：{metrics.get('sample_shortfall', 0)} 段"),
        f"种子：{cal.get('seed')}；模型：{cal.get('model')}；"
        f"端点：{cal.get('endpoint_host') or '(dry)'}",
        "",
        "章节块覆盖：",
    ]
    for block in metrics.get("block_coverage", []):
        lines.append(f"  块{block['index'] + 1:02d} 章{block['chapter_start'] + 1}-"
                     f"{block['chapter_end']}：{block['n_sample']}/{block['n_body']} "
                     f"({block['coverage']:.1%})")
    lines.append("")
    lines.append("分裂/升级：")
    for key in sorted(buckets):
        b = buckets[key]
        if b["stable"]:
            continue
        lines.append(f"  {key}：n_book={b['n_book']} n_sample={b['n_sample']} "
                     f"entropy={b['entropy']:.3f} escalated={b['escalated']}"
                     f"{' rare' if b['rare'] else ''}")
    lines.append("")
    lines.append("规则误差汇总：")
    rule_errors = metrics.get("rule_errors", {})
    totals = rule_errors.get("totals", {})
    lines.append(f"  none={totals.get('none', 0)} over={totals.get('over', 0)} "
                 f"under={totals.get('under', 0)}")
    for key in sorted(rule_errors.get("by_bucket", {})):
        counts = rule_errors["by_bucket"][key]
        if counts.get("over") or counts.get("under"):
            lines.append(f"  {key}：over={counts.get('over', 0)} "
                         f"under={counts.get('under', 0)}")
    lines.append("")
    lines.append("桶表：")
    lines.append(_bucket_table_text(buckets))
    return "\n".join(lines)


def _write_calibration(analyzed: Path, endpoint: str, model: str, seed: int,
                       plan: dict, buckets: dict[str, dict], labels: list[dict],
                       escalation_labels: list[dict], stats: dict,
                       before_after: dict, quote_rate: float) -> dict:
    disagreements, rule_errors, _ = _diagnose_labels(
        labels, escalation_labels, {ref["pid"]: ref for ref in plan["refs"]})
    blocks = []
    for block in plan["blocks"]:
        body = block["n_body"]
        blocks.append({
            **block,
            "coverage": (block["n_sample"] / body) if body else 0.0,
        })
    for key, count in stats.get("escalated_by_bucket", {}).items():
        if key in buckets:
            buckets[key]["escalated"] = count
    pending_map = stats.get("pending_by_bucket", {})
    for key, bucket in buckets.items():
        bucket["pending"] = bool(pending_map.get(key, False))
    no_signal = sum(1 for ref in plan["refs"] if abs(ref["score"] - 0.55) < 1e-9)
    metrics = {
        "sample_coverage": (stats["sample_valid"] / plan["n_paras"])
        if plan["n_paras"] else 0.0,
        "individual_judgments": (
            stats.get("sample_valid", 0) + stats.get("escalated_valid", 0)),
        "individual_coverage": (
            (stats.get("sample_valid", 0) + stats.get("escalated_valid", 0))
            / plan["n_paras"]) if plan["n_paras"] else 0.0,
        "bucket_coverage": (
            stats.get("reviewed", 0) / plan["n_paras"]
        ) if plan["n_paras"] else 0.0,
        "sample_target": plan.get("target", 0),
        "sample_shortfall": plan.get("shortfall", 0),
        "block_coverage": blocks,
        "tier_before": before_after["tier_before"],
        "tier_after": before_after["tier_after"],
        "label_distribution": _p_global(labels),
        "quote_failure_rate": quote_rate,
        "requested_items": stats.get("requested_items", stats.get("asked", 0)),
        "quote_invalid_items": stats.get("quote_invalid_items", 0),
        "first_pass_items": stats.get("first_pass_items", 0),
        "first_pass_batches": stats.get("first_pass_batches", 0),
        "first_pass_rate": stats.get("first_pass_rate", 0.0),
        "contract_retries": stats.get("contract_retries", 0),
        "label_guard": stats.get("label_guard", {}),
        "uncalibrated_blocks": [
            block["index"] for block in blocks
            if block["n_body"] > 0 and block["n_sample"] == 0
        ],
        "escalation_pending": stats.get("escalation_pending", 0),
        "rule_errors": rule_errors,
        "no_signal": no_signal,
        "sample_buckets": plan.get("buckets", []),
    }
    cal = {
        "version": 1,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "endpoint_host": _endpoint_host(endpoint),
        "model": model,
        "prompt_hash": _hash(SYSTEM_PROMPT),
        "seed": seed,
        "population_hash": plan["population_hash"],
        "n_paras": plan["n_paras"],
        "n_sample": stats["sample_valid"],
        "n_escalated": stats.get("escalated_asked", 0),
        "sample_pids": plan["sample_pids"],
        "buckets": [buckets[key] for key in sorted(buckets)],
        "metrics": metrics,
        "disagreements": disagreements,
    }
    target = _calibration_path(analyzed)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(cal, ensure_ascii=False, indent=2), encoding="utf-8")
    _report_path(analyzed).write_text(_human_report(cal, buckets), encoding="utf-8")
    return cal


def run(analyzed: Path, endpoint: str = "", model: str = "", key: str = "",
        budget: int = DEFAULT_BUDGET, seed: int = DEFAULT_SEED,
        batch: int = BATCH, concurrency: int = CONCURRENCY,
        dry: bool = False) -> dict:
    """完整标定流程。返回给 engine/server 的统计；失败批次数在 failed。"""
    if int(budget) <= 0:
        raise ReviewError("复核预算必须大于 0。")
    doc = load(analyzed)
    _ensure_score_rule(doc)
    # 至少给升级阶段留一半预算；800 时抽样仍为 400，旧客户端发 400 时
    # 抽样最多 200，不会再把升级预算吃成 0。
    sample_cap = min(DEFAULT_SAMPLE, max(1, int(budget) // 2))
    plan = plan_sample(doc, budget=budget, seed=seed, sample_cap=sample_cap)
    if dry:
        public_plan = {key: value for key, value in plan.items()
                       if key not in {"refs", "items"}}
        return {
            "dry": True, "sample": public_plan, "asked": plan["n_sample"],
            "chars": plan["chars"], "n_sample": plan["n_sample"],
            "n_escalated": 0, "failed": 0,
            "target": plan["target"], "shortfall": plan["shortfall"],
        }
    if not key:
        raise ReviewError("没有 key：AI 标定是必经的一层，出书必须过它。")
    if not plan["refs"]:
        # 没有正文可标定：不联网，明确记一个空结果。
        buckets: dict[str, dict] = {}
        before_after = {"tier_before": {}, "tier_after": {}}
        _write_calibration(analyzed, endpoint, model, seed, plan, buckets,
                           [], [], {"sample_valid": 0}, before_after, 0.0)
        return {"asked": 0, "chars": 0, "n_sample": 0, "n_escalated": 0,
                "reviewed": 0, "failed": 0,
                "calibration": str(_calibration_path(analyzed))}

    _write_progress(stage="sample", sampled=plan["n_sample"], total=plan["n_sample"],
                    reviewed=0, failed=0)
    sample_labels, sample_stats = _ask_refs(
        plan["items"], endpoint, model, key, batch, "ask",
        concurrency=concurrency)
    if sample_stats["quote_failures"] / max(1, sample_stats["asked"]) > MAX_QUOTE_FAIL_RATE:
        raise ReviewError("引用校验失败率超过 5%，这次不出书。")
    buckets, prior = fit_buckets(plan, sample_labels)
    _write_progress(stage="fit", sampled=plan["n_sample"], fitted=len(buckets),
                    total=len(buckets), reviewed=len(sample_labels),
                    failed=sample_stats["failed"])

    escalation_budget = max(0, int(budget) - plan["n_sample"])
    escalation_refs, escalated_by_bucket = plan_escalation(
        plan, buckets, escalation_budget, seed)
    escalation_labels: list[dict] = []
    escalation_stats = {"asked": 0, "failed": 0, "quote_failures": 0,
                        "chars": 0, "batches": 0, "reviewed": 0,
                        "first_pass_items": 0, "first_pass_batches": 0,
                        "contract_retries": 0}
    if escalation_refs:
        _write_progress(stage="escalate", sampled=plan["n_sample"],
                        fitted=len(buckets), escalated=0,
                        total=len(escalation_refs), reviewed=0,
                        failed=sample_stats["failed"])
        escalation_labels, escalation_stats = _ask_refs(
            escalation_refs, endpoint, model, key, batch, "escalate",
            concurrency=concurrency)
        if escalation_stats["quote_failures"] / max(1, escalation_stats["asked"]) > MAX_QUOTE_FAIL_RATE:
            raise ReviewError("升级阶段引用校验失败率超过 5%，这次不出书。")
    guard = _label_guard(sample_labels + escalation_labels, buckets)
    if guard["degenerate"]:
        raise ReviewError(
            f"模型退化：{guard['dominant_label']} 占 {guard['max_share']:.1%}，"
            f"且 {guard['eligible_buckets']} 个有效桶中 "
            f"{guard['bucket_agreement']:.0%} 主标签一致，这次不出书。")

    before_after = {"tier_before": {}, "tier_after": {}}
    applied = apply_calibration(doc, plan, buckets, escalation_labels)
    before_after.update(applied)
    analyzed.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    pending = sum(1 for chapter in doc.get("chapters", [])
                  for para in chapter.get("paragraphs", [])
                  if para.get("cal", {}).get("pending"))
    stats = {
        "asked": sample_stats["asked"] + escalation_stats["asked"],
        "chars": sample_stats["chars"] + escalation_stats["chars"],
        "batches": sample_stats["batches"] + escalation_stats["batches"],
        "reviewed": applied["reviewed"],
        "failed": sample_stats["failed"] + escalation_stats["failed"],
        "sample_valid": len(sample_labels),
        "escalated_asked": len(escalation_refs),
        "escalated_valid": len(escalation_labels),
        "escalated_by_bucket": escalated_by_bucket,
        "escalation_pending": pending,
        "n_sample": plan["n_sample"],
        "n_escalated": len(escalation_refs),
        "quote_failure_rate": (
            sample_stats["quote_failures"] + escalation_stats["quote_failures"]
        ) / max(1, sample_stats["asked"] + escalation_stats["asked"]),
        "requested_items": sample_stats["asked"] + escalation_stats["asked"],
        "quote_invalid_items": (
            sample_stats["quote_failures"] + escalation_stats["quote_failures"]),
        "first_pass_items": (
            sample_stats.get("first_pass_items", 0)
            + escalation_stats.get("first_pass_items", 0)),
        "first_pass_batches": (
            sample_stats.get("first_pass_batches", 0)
            + escalation_stats.get("first_pass_batches", 0)),
        "contract_retries": (
            sample_stats.get("contract_retries", 0)
            + escalation_stats.get("contract_retries", 0)),
        "first_pass_rate": (
            sample_stats.get("first_pass_items", 0)
            + escalation_stats.get("first_pass_items", 0)
        ) / max(1, sample_stats["asked"] + escalation_stats["asked"]),
        "label_guard": guard,
        "prior": prior,
        "pending_by_bucket": applied["pending_by_bucket"],
    }
    _write_calibration(
        analyzed, endpoint, model, seed, plan, buckets, sample_labels,
        escalation_labels, stats, before_after, stats["quote_failure_rate"])
    _write_progress(stage="apply", sampled=plan["n_sample"], fitted=len(buckets),
                    escalated=len(escalation_refs),
                    total=plan["n_sample"] + len(escalation_refs),
                    done=plan["n_sample"] + len(escalation_refs),
                    reviewed=applied["reviewed"],
                    failed=stats["failed"], pending=pending)
    return {
        **stats,
        "calibration": str(_calibration_path(analyzed)),
        "buckets": list(buckets.values()),
        "n_paras": plan["n_paras"],
        "coverage": stats["sample_valid"] / max(1, plan["n_paras"]),
    }


def probe(endpoint: str, model: str, key: str, timeout: int = 45) -> tuple[bool, str]:
    """发一次最小请求，确认新五分类契约能用；不碰文件。"""
    item = {
        "i": 0,
        "text": "他握紧拳头，决定今晚就动手。",
        "prev": "",
        "next": "",
        "chapter": "测试",
        "rule": {"score": 0.55, "flags": [], "anchors": [], "evidence": []},
    }
    try:
        raw, err = _call_with_network_retries(
            [item], endpoint, model, key, timeout=timeout)
        if raw is None:
            raise ValueError(err or "请求失败")
        valid, verr, _ = validate_reply([item], raw)
        if valid is None:
            raise ValueError(verr)
    except urllib.error.HTTPError as exc:
        hint = {
            401: "密钥不对，或这个密钥没有权限（401）",
            403: "密钥被拒绝（403）",
            404: "接口地址不对，一般要填到 /v1 这一层（404）",
            429: "额度用完或请求太频繁（429）",
        }.get(exc.code, f"服务返回了 {exc.code}")
        return False, f"没连上：{hint}。"
    except urllib.error.URLError as exc:
        return False, f"没连上：网络或域名有问题（{exc.reason}）。"
    except ValueError as exc:
        return False, f"连上了，但返回的不是五分类约定（{exc}）。"
    except OSError as exc:
        return False, f"没连上：{exc}。"
    return True, f"已连上 {model}，五分类契约试判通过。"


def main() -> None:
    ap = argparse.ArgumentParser(prog="ai_layer.py",
                                 description="AI 标定 + 定向升级（五分类）")
    ap.add_argument("src", nargs="?", help="analyzed.json（--probe / --show-rules 时可省略）")
    ap.add_argument("--endpoint", default="https://api.openai.com/v1")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--key", default=os.environ.get("WNF_AI_KEY", ""))
    ap.add_argument("--max", "--budget", dest="budget", type=int, default=DEFAULT_BUDGET,
                    help="总预算（段），默认 %d" % DEFAULT_BUDGET)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY,
                    help="同时发出的 AI 批次数，默认 %d" % CONCURRENCY)
    ap.add_argument("--lo", type=float, default=None, help="兼容旧参数，主流程忽略")
    ap.add_argument("--hi", type=float, default=None, help="兼容旧参数，主流程忽略")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--probe", action="store_true", help="只测连接，不处理文件")
    ap.add_argument("--show-rules", action="store_true", help="打印新五分类提示词")
    args = ap.parse_args()

    if args.show_rules:
        print(SYSTEM_PROMPT)
        return
    if args.probe:
        ok, msg = probe(args.endpoint, args.model, args.key)
        print(json.dumps({"ok": ok, "message": msg}, ensure_ascii=False))
        return
    if not args.src:
        print("要给一个 analyzed.json。", file=sys.stderr)
        raise SystemExit(2)
    if not args.dry and not args.key:
        print("没有 key：AI 标定是必经的一层，出书必须过它。", file=sys.stderr)
        raise SystemExit(2)
    try:
        stats = run(Path(args.src), args.endpoint, args.model, args.key,
                    budget=args.budget, seed=args.seed, batch=max(1, args.batch),
                    concurrency=max(1, min(MAX_CONCURRENCY, args.concurrency)),
                    dry=args.dry)
    except ReviewError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if args.dry and stats.get("sample"):
        print(format_sample_plan(stats["sample"]))
    elif stats.get("buckets"):
        print(_bucket_table_text({b["key"]: b for b in stats["buckets"]}))
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
