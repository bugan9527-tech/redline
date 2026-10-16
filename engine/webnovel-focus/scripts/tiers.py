"""kind + score → 默认分层。

引擎不替读者下审美判断：这里给出的只是「默认处置建议」，
渲染层会按读者偏好覆盖它。所有消费者共用这一份映射。
"""

from __future__ import annotations

# 零信息层：不管什么口味都是噪音，引擎可判死
ZERO_KINDS = {"author_note", "watermark", "punctuation"}
# 结构元素：不是内容，渲染成分隔线而不是文字
SEPARATOR_KINDS = {"separator"}

TIER_LIMITS = (
    ("zero", 0.12),
    ("fade", 0.38),
    ("weak", 0.52),
    ("mid", 0.68),
)


def tier_of(kind: str, score: float) -> str:
    if kind in SEPARATOR_KINDS:
        return "sep"
    if kind in ZERO_KINDS:
        return "zero"
    for name, limit in TIER_LIMITS:
        if score < limit:
            return name
    return "plot"
