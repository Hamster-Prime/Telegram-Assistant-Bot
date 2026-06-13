"""token 估算 —— 中文≈1.5字/token、英文≈4字符/token 的混合估算。"""
from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数:CJK 字符按 1/1.5,其余按 1/4。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿" or "　" <= ch <= "〿")
    other = len(text) - cjk
    return max(1, int(cjk / 1.5 + other / 4))


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """按估算预算截断文本(对长网页正文等使用)。"""
    if estimate_tokens(text) <= max_tokens:
        return text
    # 二分截断
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "\n…(已截断)"
