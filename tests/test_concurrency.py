"""并发原语测试 —— 信号量背压、按用户限并发、限流器、任务注册表。"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.core.concurrency import (
    ConcurrencyGuard,
    SendRateLimiter,
    TaskRegistry,
    UserLock,
)


async def test_global_chat_slots_block():
    """全局槽=2:第 3 个请求排队,先完成者释放后才进入。"""
    guard = ConcurrencyGuard(max_chats=2, max_generations=1, per_user=10)
    running, order = 0, []
    peak = 0

    async def job(uid: int, dur: float):
        nonlocal running, peak
        async with guard.chat_slot(uid):
            running += 1
            peak = max(peak, running)
            order.append(f"start{uid}")
            await asyncio.sleep(dur)
            running -= 1
            order.append(f"end{uid}")

    await asyncio.gather(job(1, 0.05), job(2, 0.05), job(3, 0.05))
    assert peak == 2  # 永不超过全局上限


async def test_per_user_limit():
    """单用户并发上限=2:同一用户第 3 个任务等待,不同用户不受影响。"""
    guard = ConcurrencyGuard(max_chats=10, max_generations=1, per_user=2)
    user1_peak, user1_run = 0, 0

    async def u1_job():
        nonlocal user1_peak, user1_run
        async with guard.chat_slot(1):
            user1_run += 1
            user1_peak = max(user1_peak, user1_run)
            await asyncio.sleep(0.03)
            user1_run -= 1

    other_done = False

    async def u2_job():
        nonlocal other_done
        async with guard.chat_slot(2):
            other_done = True

    await asyncio.gather(u1_job(), u1_job(), u1_job(), u2_job())
    assert user1_peak == 2  # 用户1 不超限
    assert other_done       # 用户2 不被饿死


async def test_same_user_parallel_within_limit():
    """同一用户的 2 个请求(≤上限)真正并行。"""
    guard = ConcurrencyGuard(max_chats=10, max_generations=1, per_user=3)
    t0 = time.monotonic()

    async def job():
        async with guard.chat_slot(1):
            await asyncio.sleep(0.05)

    await asyncio.gather(job(), job())
    elapsed = time.monotonic() - t0
    assert elapsed < 0.09  # 并行:约 0.05s,而非 0.10s


async def test_send_rate_limiter():
    """限流器:速率 50/s 下发送 10 条耗时 ≥ ~0.18s。"""
    limiter = SendRateLimiter(rate_per_sec=50)
    t0 = time.monotonic()
    for _ in range(60):
        await limiter.acquire()
    elapsed = time.monotonic() - t0
    # 初始桶容量 50,再补 10 个 → 至少 ~0.2s
    assert elapsed >= 0.15


async def test_task_registry_tracks_and_shuts_down():
    reg = TaskRegistry()
    started = asyncio.Event()

    async def forever():
        started.set()
        await asyncio.sleep(999)

    reg.spawn(forever(), name="t1")
    await started.wait()
    assert reg.count == 1
    await reg.shutdown()
    assert reg.count == 0


async def test_task_registry_logs_crash():
    reg = TaskRegistry()

    async def boom():
        raise RuntimeError("炸了")

    t = reg.spawn(boom(), name="boom")
    await asyncio.sleep(0.05)
    assert reg.count == 0  # 异常任务被回收且不挂掉进程


async def test_user_lock_serializes():
    locks = UserLock()
    order = []

    async def critical(tag: str):
        async with locks.for_user(1):
            order.append(f"{tag}-in")
            await asyncio.sleep(0.02)
            order.append(f"{tag}-out")

    await asyncio.gather(critical("a"), critical("b"))
    # 串行:in/out 成对出现,不交错
    assert order in (["a-in", "a-out", "b-in", "b-out"],
                     ["b-in", "b-out", "a-in", "a-out"])
