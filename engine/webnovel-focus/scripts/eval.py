"""抽样审查：把某一层的段落连同上文一起打出来，人工判断判定是否合理。

用固定随机种子，所以同一份 analyzed.json 每次抽到的是同一批，便于改规则后对比。

用法:
    python eval.py <analyzed.json> --tier plot [--n 25] [--seed 7]
    python eval.py <analyzed.json> --tier mid  --context 1

``sample(doc, tier=...)`` 返回同样的文本，供 run.py 串流程用。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tiers import tier_of  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass


def sample(doc: dict, tier: str = "plot", n: int = 25, seed: int = 7,
           context: int = 1, chapter: int = 0) -> str:
    """按固定种子从某一层抽段，连同证据和上下文一起打出来。"""
    flat = [(ch["index"], i, p) for ch in doc["chapters"] for i, p in enumerate(ch["paragraphs"])]

    pool = [x for x in flat if tier_of(x[2]["kind"], x[2]["score"]) == tier]
    if chapter:
        pool = [x for x in pool if x[0] == chapter]
    if not pool:
        return f"没有 {tier} 层的段落"

    random.seed(seed)
    picks = random.sample(pool, min(n, len(pool)))
    picks.sort(key=lambda x: (x[0], x[1]))

    by_ch = {ch["index"]: ch for ch in doc["chapters"]}
    lines = [f"# {tier} 层抽样 {len(picks)}/{len(pool)} 段 (seed={seed})\n"]
    for ci, pi, p in picks:
        paras = by_ch[ci]["paragraphs"]
        lines.append(f"===== 第{ci}章 第{pi}段  score={p['score']}  {p['flags']} =====")
        if context:
            for k in range(max(0, pi - context), pi):
                lines.append(f"  前 | {paras[k]['text'][:88]}")
        lines.append(f"  >>> {p['text']}")
        for e in p["evidence"]:
            lines.append(f"  证据: {e}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--tier", default="plot")
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--context", type=int, default=1)
    ap.add_argument("--chapter", type=int, default=0)
    ap.add_argument("-o", default=None)
    args = ap.parse_args()

    doc = json.loads(Path(args.src).read_text(encoding="utf-8"))
    text = sample(doc, tier=args.tier, n=args.n, seed=args.seed,
                  context=args.context, chapter=args.chapter)
    if args.o:
        Path(args.o).write_text(text, encoding="utf-8")
        print(f"wrote {args.o}")
    else:
        print(text)


if __name__ == "__main__":
    main()
