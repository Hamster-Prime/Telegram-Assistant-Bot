"""并发原语 ★ —— 信号量背压、按用户锁/限并发、Telegram 发送限流、后台任务注册表。

层次(plan §4):
- L3 有界资源:MAX_CONCURRENT_CHATS / MAX_CONCURRENT_GENERATIONS / PER_USER_CONCURRENCY
- L4 共享安全:按 user_id 键控的轻量锁
- L5 生命周期:任务注册表统一跟踪 create_task,优雅关停时取消/等待
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Coroutine
from typing import Any

from app.logging import get_logger

log = get_logger("core.concurrency")


class ConcurrencyGuard:
    """全局 + 按用户的并发槽位管理(背压:排队等待而非拒绝)。"""

    def __init__(self, max_chats: int, max_generations: int, per_user: int) -> None:
        self._chat_sem = asyncio.Semaphore(max_chats)
        self._gen_sem = asyncio.Semaphore(max_generations)
        self._per_user_limit = per_user
        self._user_sems: dict[int, asyncio.Semaphore] = {}
        self._user_active: dict[int, int] = defaultdict(int)
        # 引用计数:多少个 _SlotCtx 当前引用着该用户的信号量(含排队等待中的)。
        # 归零时才回收信号量,避免「等待者还引用着、却被另一路回收」的竞态。
        self._user_refs: dict[int, int] = defaultdict(int)

    def _acquire_user_sem(self, user_id: int) -> asyncio.Semaphore:
        """取(或建)用户信号量,并登记一次引用。"""
        sem = self._user_sems.get(user_id)
        if sem is None:
            sem = asyncio.Semaphore(self._per_user_limit)
            self._user_sems[user_id] = sem
        self._user_refs[user_id] += 1
        return sem

    def _release_user_ref(self, user_id: int) -> None:
        """归还一次引用;无引用时回收信号量与计数,防无界增长。"""
        self._user_refs[user_id] -= 1
        if self._user_refs[user_id] <= 0:
            self._user_refs.pop(user_id, None)
            self._user_sems.pop(user_id, None)
            self._user_active.pop(user_id, None)

    def chat_slot(self, user_id: int) -> _SlotCtx:
        """对话槽位:全局 + 单用户双重信号量。"""
        return _SlotCtx(self, self._chat_sem, user_id, kind="对话")

    def generation_slot(self, user_id: int) -> _SlotCtx:
        """生成槽位(视频/音乐等昂贵操作)。"""
        return _SlotCtx(self, self._gen_sem, user_id, kind="生成")

    def is_busy(self, sem_kind: str = "chat") -> bool:
        """槽位是否已满(用于占位消息提示"排队中")。"""
        sem = self._chat_sem if sem_kind == "chat" else self._gen_sem
        return sem.locked()

    def user_active_count(self, user_id: int) -> int:
        return self._user_active[user_id]


class _SlotCtx:
    def __init__(self, guard: ConcurrencyGuard, global_sem: asyncio.Semaphore,
                 user_id: int, kind: str) -> None:
        self._guard = guard
        self._global_sem = global_sem
        # 登记引用(含排队期):退出时归还,归零回收 → 杜绝按 user_id 无界增长
        self._user_sem = guard._acquire_user_sem(user_id)
        self._user_id = user_id
        self._kind = kind
        self._t0 = 0.0

    async def __aenter__(self) -> _SlotCtx:
        self._t0 = time.monotonic()
        queued = self._global_sem.locked() or self._user_sem.locked()
        if queued:
            log.info("并发槽位已满,进入排队", 类型=self._kind, 用户=self._user_id,
                     用户当前并发=self._guard._user_active[self._user_id])
        # 先取用户槽(防单用户占满全局),再取全局槽
        await self._user_sem.acquire()
        try:
            await self._global_sem.acquire()
        except BaseException:
            self._user_sem.release()
            raise
        self._guard._user_active[self._user_id] += 1
        wait_ms = round((time.monotonic() - self._t0) * 1000)
        if wait_ms > 100:
            log.info("并发槽位已获取(经排队)", 类型=self._kind, 用户=self._user_id,
                     排队耗时毫秒=wait_ms)
        return self

    async def __aexit__(self, *exc) -> None:
        self._guard._user_active[self._user_id] -= 1
        if self._guard._user_active[self._user_id] <= 0:
            self._guard._user_active.pop(self._user_id, None)
        self._global_sem.release()
        self._user_sem.release()
        # 归还引用(可能触发回收)。必须在 release 之后,确保信号量状态已复原。
        self._guard._release_user_ref(self._user_id)


class UserLock:
    """按 user_id 的轻量异步锁 —— 仅串行化关键段(如配额结算、/reset)。

    用完即回收(无人持有/等待时移除),避免按 user_id 无界增长。
    """

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._refs: dict[int, int] = defaultdict(int)

    def for_user(self, user_id: int) -> _TrackedLock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return _TrackedLock(self, user_id, lock)


class _TrackedLock:
    """包装 asyncio.Lock 的异步上下文:进入登记引用,退出归还,归零回收。"""

    def __init__(self, owner: UserLock, user_id: int, lock: asyncio.Lock) -> None:
        self._owner = owner
        self._user_id = user_id
        self._lock = lock
        owner._refs[user_id] += 1

    async def __aenter__(self) -> asyncio.Lock:
        await self._lock.acquire()
        return self._lock

    async def __aexit__(self, *exc) -> None:
        self._lock.release()
        self._owner._refs[self._user_id] -= 1
        if self._owner._refs[self._user_id] <= 0:
            self._owner._refs.pop(self._user_id, None)
            self._owner._locks.pop(self._user_id, None)


class SendRateLimiter:
    """全局 Telegram 发送限流器(令牌桶,≈30 msg/s 留余量)。

    所有出站 sendMessage/editMessageText 前 await acquire()。
    """

    def __init__(self, rate_per_sec: float) -> None:
        self._rate = max(1.0, rate_per_sec)
        self._tokens = self._rate
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now_t = time.monotonic()
                self._tokens = min(self._rate, self._tokens + (now_t - self._last) * self._rate)
                self._last = now_t
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self._rate
                await asyncio.sleep(wait)


class TaskRegistry:
    """后台任务注册表(L5)—— 跟踪所有 create_task,优雅关停统一取消。"""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str = "") -> asyncio.Task:
        task = asyncio.create_task(coro, name=name or None)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("后台任务异常退出", 任务名=task.get_name(), 异常类型=type(exc).__name__,
                      异常信息=str(exc))

    @property
    def count(self) -> int:
        return len(self._tasks)

    async def shutdown(self, timeout: float = 10.0) -> None:
        if not self._tasks:
            return
        log.info("正在关停后台任务", 任务数=len(self._tasks))
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        log.info("后台任务已全部关停")
