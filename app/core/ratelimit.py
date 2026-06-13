"""编辑节流 —— editMessageText 路径的增量缓冲与提交判定(plan §11)。

提交条件(任一满足):
- 距上次提交 ≥ EDIT_THROTTLE_MS
- 新增 ≥ 80 字符
- 遇句末标点
- 流结束(强制)
文本未变化不提交(防 "message is not modified")。
"""
from __future__ import annotations

import time

_SENTENCE_ENDINGS = "。!?!?…\n"


class EditThrottle:
    def __init__(self, throttle_ms: int = 1500, min_delta_chars: int = 80) -> None:
        self._interval = throttle_ms / 1000
        self._min_delta = min_delta_chars
        self._last_commit_t = 0.0
        self._last_committed_text = ""

    def should_commit(self, current_text: str, *, final: bool = False) -> bool:
        if current_text == self._last_committed_text:
            return False  # 无变化不提交
        if final:
            return True
        now_t = time.monotonic()
        elapsed = now_t - self._last_commit_t
        delta = len(current_text) - len(self._last_committed_text)
        if elapsed >= self._interval:
            return True
        if delta >= self._min_delta:
            return True
        if current_text and current_text[-1] in _SENTENCE_ENDINGS and elapsed >= self._interval / 3:
            return True
        return False

    def mark_committed(self, text: str) -> None:
        self._last_commit_t = time.monotonic()
        self._last_committed_text = text

    @property
    def last_text(self) -> str:
        return self._last_committed_text
