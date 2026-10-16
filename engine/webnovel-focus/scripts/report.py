"""把分析结果打印成人能读的对照表，用于检查判定是否合理。

``render_report(doc, full=False)`` 返回整份对照表，供 run.py 串流程用；
命令行仍然可以直接跑：

用法:
    python report.py <analyzed.json> [--full] [-o report.txt]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tiers import tier_of  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass


def cell(text: str, width: int) -> str:
    out, n = "", 0
    for ch in text:
        w = 2 if ord(ch) > 0x2E80 else 1
        if n + w > width:
            break
        out += ch
        n += w
    return out + " " * (width - n)


def render_report(doc: dict, full: bool = False) -> str:
    """逐段对照表 + 分层统计。--full 打全文，否则每段截到 34 字。"""
    lines = [f"人名候选: {', '.join(doc.get('names', [])) or '(无)'}"]
    counts: dict[str, int] = {}

    for ch in doc["chapters"]:
        lines.append(f"\n=== {ch['title']}  ({len(ch['paragraphs'])} 段)")
        for p in ch["paragraphs"]:
            t = tier_of(p["kind"], p["score"])
            counts[t] = counts.get(t, 0) + 1
            marks = ",".join(p["flags"]) or "-"
            anchors = ",".join(p["anchors"]) or "-"
            body = p["text"] if full else cell(p["text"], 34)
            lines.append(
                f"{p['pid']:>7} {t:>5} {p['kind']:<11} {p['score']:.2f} "
                f"{cell(marks, 20)} {cell(anchors, 22)} {body}"
            )
            for e in p["evidence"]:
                lines.append(f"{'':>7} └ {e}")

    total = sum(counts.values())
    lines.append("\n--- 分层统计 ---")
    for t in ("plot", "mid", "weak", "fade", "zero"):
        n = counts.get(t, 0)
        if n:
            lines.append(f"{t:>5}: {n:>3} 段  {n / total:>5.1%}")
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: report.py <analyzed.json> [--full] [-o out.txt]")
        raise SystemExit(2)

    src = Path(sys.argv[1])
    full = "--full" in sys.argv
    out = Path(sys.argv[sys.argv.index("-o") + 1]) if "-o" in sys.argv else None

    doc = json.loads(src.read_text(encoding="utf-8"))
    text = render_report(doc, full=full)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
