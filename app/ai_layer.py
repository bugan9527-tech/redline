"""AI 复核层：只碰模糊带，不碰引擎。

引擎是纯本地规则，没有 LLM。这一层是必经的一层（不是可选开关）：
把规则判不准的那一段分数区间（默认 0.30 ~ 0.70）挑出来，交给用户自己的
模型复核一遍，结果按 analyzed.json 原有的 schema 写回去（改的是
每个段落的 score 与 evidence，kind / flags / anchors 一律不动）。

关键点：喂给模型的判定标准，是从 webnovel-focus 这个 skill 自己写的
references/ 里**只读**摘出来的（三层处置权 + 分层阈值）。所以是"AI 按这个
skill 的口径打分"，而不是按另一套自编规则。

用法:
    python ai_layer.py <analyzed.json> --endpoint URL --model NAME --key SK [--lo 0.3] [--hi 0.7]
                                       [--max 400] [--batch 16] [--dry]

    --dry         只挑段落、不联网，打印将要发出去多少段、多少字。
    --probe       只发一次最小请求，测地址 / Key / 模型通不通，不碰文件。
    --show-rules  打印实际喂给模型的判定标准（给人看的）。
    没有 key 时：直接报错退出——出书必须过这一层，不静默退回纯规则。

Key 只在这一次请求里用，不写进任何文件，也不进日志。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

AMBIG_LO, AMBIG_HI = 0.30, 0.70
MAX_PARAS = 400
BATCH = 16

ENGINE = Path(__file__).resolve().parent.parent / "engine" / "webnovel-focus"


def _write_progress(**kw) -> None:
    """把实时进度写给服务端看（路径来自 WNF_PROGRESS）。纯附加，失败也不打断复核。"""
    path = os.environ.get("WNF_PROGRESS", "")
    if not path:
        return
    try:
        Path(path).write_text(json.dumps(kw, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


BASE_SYSTEM = (
    "你是中文网文的编辑。给你若干段正文，每段给一个 0~1 的分数，"
    "表示这一段承载故事推进的程度：0 是纯铺垫、写景、复述、凑字，"
    "1 是关键情节（人物做出决定、信息揭开、关系改变、冲突发生）。"
    "不要改写原文，不要解释文学价值，只打分。"
    '只输出一个 JSON 数组，每项形如 {"i": 序号, "score": 0.0~1.0, "why": "不超过12字的理由"}。'
    "序号必须和输入一一对应，不要漏项。"
)

# 从 skill 的 references/ 里摘哪两节：三层处置权（怎么定性）+ 分层阈值（分数打到哪一档）
_RUBRIC_SECTIONS = (
    ("references/judgment-rules.md", "## 一、三层处置权", "## 二、"),
    ("references/render-spec.md", "## 一、分层 → 处置映射", "## 二、"),
)
_RUBRIC_CACHE: str | None = None


def _slice(text: str, start: str, end: str) -> str:
    i = text.find(start)
    if i < 0:
        return ""
    j = text.find(end, i + len(start))
    return text[i: j if j > 0 else len(text)].strip()


def skill_rubric() -> str:
    """只读摘出 webnovel-focus 自己写的判定标准，拼进系统提示词。

    这样模型是"按这个 skill 的口径"打分，而不是按另写的一套。skill 只读，不写入。
    """
    global _RUBRIC_CACHE
    if _RUBRIC_CACHE is not None:
        return _RUBRIC_CACHE
    parts: list[str] = []
    for rel, start, end in _RUBRIC_SECTIONS:
        try:
            txt = (ENGINE / rel).read_text(encoding="utf-8")
        except OSError:
            continue
        sec = _slice(txt, start, end)
        if sec:
            parts.append(sec)
    _RUBRIC_CACHE = "\n\n".join(parts)
    return _RUBRIC_CACHE


def system_prompt() -> str:
    """系统提示词 = 通用打分说明 + skill 的判定标准（读不到时退回前者）。"""
    rubric = skill_rubric()
    if not rubric:
        return BASE_SYSTEM
    return (BASE_SYSTEM
            + "\n\n以下是 webnovel-focus 这个 skill 自己写的判定标准，请严格按这个口径打分：\n"
            + rubric)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def pick(doc: dict, lo: float, hi: float, cap: int) -> list[tuple[int, int]]:
    """挑出模糊带的正文段（位置下标，按原顺序）。"""
    hits: list[tuple[int, int]] = []
    for ci, ch in enumerate(doc.get("chapters", [])):
        for pi, p in enumerate(ch.get("paragraphs", [])):
            if p.get("kind") != "body":
                continue
            score = p.get("score")
            if score is None or not (lo <= score <= hi):
                continue
            hits.append((ci, pi))
            if len(hits) >= cap:
                return hits
    return hits


def ask(batch: list[str], endpoint: str, model: str, key: str, timeout: int = 90) -> list[dict]:
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": json.dumps(
                [{"i": i, "text": t} for i, t in enumerate(batch)], ensure_ascii=False)},
        ],
    }
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", "authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8", "replace"))
    content = (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    match = re.search(r"\[[\s\S]*\]", content)
    if not match:
        raise ValueError("模型没有返回 JSON 数组")
    return json.loads(match.group(0))


def probe(endpoint: str, model: str, key: str, timeout: int = 45) -> tuple[bool, str]:
    """发一次最小请求，确认地址 / Key / 模型能不能用。不碰任何文件。"""
    try:
        items = ask(["他握紧拳头，决定今晚就动手。"], endpoint, model, key, timeout)
    except urllib.error.HTTPError as exc:
        hint = {
            401: "密钥不对，或这个密钥没有权限（401）",
            403: "密钥被拒绝（403）",
            404: "接口地址不对，一般要填到 /v1 这一层（404）",
            429: "额度用完或请求太频繁（429）",
        }.get(exc.code, f"服务返回了 {exc.code}")
        return False, f"没连上：{hint}。"
    except urllib.error.URLError as exc:
        return False, f"没连上：网络或域名有问题（{exc.reason}）。查一下地址和本机网络。"
    except ValueError as exc:
        return False, f"连上了，但返回的不是约定的 JSON（{exc}）。这个模型可能不兼容。"
    except OSError as exc:
        return False, f"没连上：{exc}。"
    if not items:
        return False, "连上了，但模型一条判定也没返回。"
    return True, f"已连上 {model}，它能按约定格式打分（试判 1 段通过）。"


def apply(doc: dict, pairs: list[tuple[int, int]], items: list[dict]) -> dict:
    """按原 schema 写回：只改 score 与 evidence，另记一条 ai 复核痕迹。"""
    hit = 0
    for (ci, pi), item in zip(pairs, items):
        try:
            score = float(item.get("score"))
        except (TypeError, ValueError):
            continue
        p = doc["chapters"][ci]["paragraphs"][pi]
        old = p.get("score")
        p["score"] = round(max(0.0, min(1.0, score)), 3)
        why = str(item.get("why") or "").strip()[:24]
        p["evidence"] = list(p.get("evidence") or []) + [f"AI 复核 {old} → {p['score']}: {why}"]
        p["ai"] = {"score": round(max(0.0, min(1.0, score)), 3), "why": why}
        hit += 1
    return {"reviewed": hit, "asked": len(pairs)}


def run(path: Path, endpoint: str, model: str, key: str, lo: float, hi: float,
        cap: int, batch: int, dry: bool) -> dict:
    doc = load(path)
    pairs = pick(doc, lo, hi, cap)
    chars = sum(len(doc["chapters"][c]["paragraphs"][p]["text"]) for c, p in pairs)
    rules = {"skill": "webnovel-focus", "rules_chars": len(skill_rubric())}
    total = (len(pairs) + batch - 1) // batch

    if dry or not pairs:
        _write_progress(stage="ai", done=total, total=total, reviewed=0, failed=0)
        return {"asked": len(pairs), "chars": chars, "batches": total,
                "reviewed": 0, "note": "dry" if dry else "模糊带为空，规则判定直接通过", **rules}

    _write_progress(stage="ai", done=0, total=total, reviewed=0, failed=0)
    reviewed = 0
    failed = 0
    for start in range(0, len(pairs), batch):
        chunk = pairs[start:start + batch]
        texts = [doc["chapters"][c]["paragraphs"][p]["text"] for c, p in chunk]
        # 网络抖一下不该让整本书重来：一批最多试三次，还不行才算这批失败。
        items = None
        err = None
        for attempt in range(3):
            try:
                items = ask(texts, endpoint, model, key)
                break
            except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError) as exc:
                err = exc
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        if items is None:
            failed += 1
            print(f"[warn] 第 {start // batch + 1} 批复核三次都没成，保留规则判定：{err}")
        else:
            stats = apply(doc, chunk, items)
            reviewed += stats["reviewed"]
        _write_progress(stage="ai", done=start // batch + 1, total=total,
                        reviewed=reviewed, failed=failed)

    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    _write_progress(stage="ai", done=total, total=total, reviewed=reviewed, failed=failed)
    return {"asked": len(pairs), "chars": chars, "batches": total,
            "reviewed": reviewed, "failed": failed, **rules}


def main() -> None:
    ap = argparse.ArgumentParser(prog="ai_layer.py",
                                description="只对模糊带段落做 AI 复核（按 webnovel-focus 的判定标准）")
    ap.add_argument("src", nargs="?", help="analyzed.json（--probe / --show-rules 时可省略）")
    ap.add_argument("--endpoint", default="https://api.openai.com/v1")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--key", default=os.environ.get("WNF_AI_KEY", ""))
    ap.add_argument("--lo", type=float, default=AMBIG_LO)
    ap.add_argument("--hi", type=float, default=AMBIG_HI)
    ap.add_argument("--max", type=int, dest="cap", default=MAX_PARAS)
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--probe", action="store_true", help="只测连接，不处理文件")
    ap.add_argument("--show-rules", action="store_true", help="打印喂给模型的判定标准")
    args = ap.parse_args()

    if args.show_rules:
        print(system_prompt())
        return

    if args.probe:
        ok, msg = probe(args.endpoint, args.model, args.key)
        print(json.dumps({"ok": ok, "message": msg}, ensure_ascii=False))
        return

    if not args.src:
        print("要给一个 analyzed.json。", file=sys.stderr)
        raise SystemExit(2)

    if not args.dry and not args.key:
        print("没有 key：AI 复核是必经的一层，出书必须过它。", file=sys.stderr)
        raise SystemExit(2)

    stats = run(Path(args.src), args.endpoint, args.model, args.key,
                args.lo, args.hi, args.cap, args.batch, args.dry)
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
