"""统一流式渲染抽象 —— Draft(私聊)/ Edit(群聊)/ Guest 三路(plan §11)。

- 私聊:sendMessageDraft 原生草稿流 → ~30s 窗口内 sendMessage 定稿
- 群聊:sendMessage 占位 → 统一轮询循环 editMessageText → 末次编辑定稿
- Guest:answerGuestQuery 一次应答入口 → 返回 inline_message_id 上轮询编辑
并发隔离:每个回答独立 draft_id / 占位消息,互不覆盖。

aiogram 3.29 原生封装 SendRichMessage / SendRichMessageDraft / AnswerGuestQuery(Bot API 10.1)。

限流约束(Telegram 官方 FAQ,多用户安全):
- 单聊 sendMessage/editMessageText ≤1 条/秒(短突发可,持续超则 429)
- 群聊 ≤20 条/分钟 ≈ 3 秒/次
- 全局合计 ≤~30 条/秒(由 SendRateLimiter 排队消化)
- sendChatAction typing 状态维持约 5 秒
- Guest bot 非会话成员,不支持 sendChatAction(仅 answerGuestQuery 通道)

★ 统一轮询门控(Edit/Guest):
Edit/Guest 渲染器用「单一后台轮询循环」按设定间隔驱动所有编辑。
update() 仅暂存最新文本,真正发编辑的是循环。每次 tick 三选一:
  ① 有新内容 → 写入正文 + 空行 + 状态行(不带光标)
  ② 内容静默(idle)→ no-op,不再发编辑(缓解 429 的核心)
  ③ 占位阶段(首条内容前)→ 渲染纯状态行(默认「正在处理 ...」)
状态行文案的切换由 set_status 驱动(Agent 在工具调用前下发),而非光标闪烁。
间隔门控保证「同一消息任意两次 edit(无论内容/状态行/定稿)距离 ≥ 设定值」:
tick 循环 wait_for 强制间隔;_do_edit 咽喉点在 HTTP 调用前记录时间戳,
finalize 紧随末次编辑时由 _ensure_interval_elapsed 补齐 sleep,杜绝 429。
"""
from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any, Protocol

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import (
    AnswerGuestQuery,
    EditMessageMedia,
    SendRichMessage,
    SendRichMessageDraft,
)
from aiogram.types import (
    InlineQueryResultArticle,
    InputMediaAudio,
    InputMediaPhoto,
    InputMediaVideo,
    InputRichMessageContent,
)

from app.core.concurrency import SendRateLimiter
from app.core.ratelimit import EditThrottle
from app.core.richmsg import (
    RICH_MESSAGE_LIMIT,
    RichAttachment,
    RichAttachmentCollector,
    clip_markdown,
    merge_attachments,
    to_rich_input,
)
from app.logging import get_logger

log = get_logger("core.streaming")

TG_MESSAGE_LIMIT = RICH_MESSAGE_LIMIT  # Rich Message 上限(32KB)

# 状态行文案:按语义分类映射工具名(供 set_status 驱动)
_SEARCH_TOOLS = frozenset({"web_search", "web_fetch"})
_GENERATE_TOOLS = frozenset({
    "generate_image", "generate_video", "synthesize_speech", "generate_music",
})
_STATUS_THINKING = "正在处理 ..."
_STATUS_TOOL_DEFAULT = "正在调用工具 ..."


def _status_for_tool(name: str) -> str:
    """工具名 → 状态行文案(按语义分类)。未知工具归「正在调用工具」。"""
    if name in _SEARCH_TOOLS:
        return "正在搜索 ..."
    if name in _GENERATE_TOOLS:
        return "正在生成 ..."
    return _STATUS_TOOL_DEFAULT


def clip(text: str, limit: int = TG_MESSAGE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


async def _sleep_retry_after(e: TelegramRetryAfter) -> None:
    log.warning("Telegram限流,按retry_after退避", 等待秒=e.retry_after)
    await asyncio.sleep(e.retry_after + 0.5)


class StreamRenderer(Protocol):
    async def start(self) -> None: ...
    async def update(self, full_text: str) -> None: ...
    async def finalize(self, full_text: str) -> int | None:
        """定稿;返回最终 message_id(可得时)。"""
        ...

    async def set_status(self, status: str) -> None:
        """设置当前状态行文案(如「正在处理 ...」)并立即渲染。

        主动发一次编辑:正文已落地则渲染「正文+状态行」,占位阶段则渲染纯状态行。
        走与轮询循环相同的间隔门控(429 安全)。DraftRenderer 是 no-op(无后缀概念)。
        """
        ...

    async def fail(self, error_text: str) -> None: ...

    @property
    def last_rendered_text(self) -> str:
        """最近一次成功渲染(发到 Telegram)的纯文本(不含光标)。

        供 agent 终稿对账:若与 result.text 不一致,说明末次编辑未落地,
        需强制重发,避免出现「语句截断」(末尾缺字)。
        """
        ...


# ── 共享助手 ─────────────────────────────────────────────────


async def _run_typing_loop(
    bot: Bot,
    chat_id: int,
    limiter: SendRateLimiter,
    refresh_s: float,
    stop_event: asyncio.Event,
) -> None:
    """周期性发送 typing 状态(status 维持约 5s,按 refresh_s 刷新)。

    typing 丢失对主流程无害(仅无状态显示),故所有异常被吞掉。
    群聊:bot 是成员,支持;私聊:bot 是对方,支持;
    Guest:bot 非成员,不支持 → 不应调用本函数。
    """
    try:
        while not stop_event.is_set():
            try:
                await limiter.acquire()
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except TelegramRetryAfter as e:
                await asyncio.sleep(min(e.retry_after + 0.3, refresh_s))
            except Exception as e:
                log.debug("typing刷新失败(忽略)", 会话=chat_id, 错误=str(e)[:100])
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=refresh_s)
            except TimeoutError:
                pass
    except asyncio.CancelledError:
        raise


async def _commit_final_edit(
    edit_callable: Any,
    text: str,
    *,
    chat_label: Any = None,
) -> bool:
    """防弹化的末次编辑 —— 保证完整文本落地,杜绝「语句截断」。

    流程:
    1. Rich Markdown 编辑(edit_callable 内部用 editMessageText(rich_message=…))
    2. 「message is not modified」→ 文本未变,视为成功
    3. Rich 解析失败 → 降级纯文本编辑(edit_callable(text, plain=True))
    4. TelegramRetryAfter → 退避重试(上限 3 次)
    5. 仍失败 → 记 error 但不再抛出(避免冒泡到 errors.py)

    edit_callable(text, *, plain=False) 由调用方提供,封装 chat/inline 差异。
    返回 True 表示完整文本已成功落地。
    """
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            await edit_callable(text)
            return True
        except TelegramRetryAfter as e:
            last_exc = e
            log.warning("定稿编辑被限流,退避重试", 标识=chat_label,
                        尝试=attempt + 1, 等待秒=e.retry_after)
            await asyncio.sleep(e.retry_after + 0.3)
            continue
        except TelegramBadRequest as e:
            msg = str(e)
            if "message is not modified" in msg:
                return True
            log.warning("定稿Rich解析失败,降级纯文本", 标识=chat_label, 错误=msg[:120])
            try:
                await edit_callable(text, plain=True)
                return True
            except TelegramRetryAfter as re_:
                last_exc = re_
                await asyncio.sleep(re_.retry_after + 0.3)
                continue
            except TelegramBadRequest as e2:
                if "message is not modified" in str(e2):
                    return True
                last_exc = e2
                continue
    log.error("定稿编辑最终失败(消息可能停留在较短的中间状态)", 标识=chat_label,
              错误=str(last_exc)[:160] if last_exc else "未知")
    return False


class DraftRenderer:
    """私聊:sendMessageDraft 原生草稿流。

    草稿是临时预览(~30s),不自动落地;必须窗口内 sendMessage 定稿。

    typing 策略:首 token 到达前发 typing 填补「无预览」的空白;
    一旦首个 draft 预览成功发出(draft 自带流式预览),停止 typing 任务。
    """

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        limiter: SendRateLimiter,
        throttle_ms: int = 1000,
        *,
        typing_refresh_s: float = 4.0,
        rich_attachments: RichAttachmentCollector | None = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._limiter = limiter
        self._draft_id = secrets.randbelow(2**31 - 1) + 1
        # 草稿更新比编辑廉价,节流间隔取 1/3
        self._throttle = EditThrottle(throttle_ms=max(300, throttle_ms // 3))
        self._typing_refresh_s = typing_refresh_s
        self._typing_stop: asyncio.Event | None = None
        self._typing_task: asyncio.Task | None = None
        self._first_token_sent = False
        self._last_rendered = ""
        self.rich_attachments = rich_attachments

    async def _send_draft(self, text: str) -> None:
        await self._bot(SendRichMessageDraft(
            chat_id=self._chat_id,
            draft_id=self._draft_id,
            rich_message=to_rich_input(text or "…"),
        ))

    async def start(self) -> None:
        log.debug("私聊不发送初始占位消息", 会话=self._chat_id, 草稿ID=self._draft_id)
        # 首 token 前用 typing 填补空白(draft 预览尚未出现)
        self._typing_stop = asyncio.Event()
        self._typing_task = asyncio.create_task(
            _run_typing_loop(self._bot, self._chat_id, self._limiter,
                             self._typing_refresh_s, self._typing_stop))

    async def _stop_typing(self) -> None:
        if self._typing_task is not None and not self._typing_task.done():
            if self._typing_stop is not None:
                self._typing_stop.set()
            self._typing_task.cancel()
            try:
                await self._typing_task
            except (asyncio.CancelledError, Exception):
                pass
        self._typing_task = None
        self._typing_stop = None

    async def set_status(self, status: str) -> None:
        """私聊不渲染状态行(原生草稿预览无后缀概念);接受调用但不做任何事。"""
        return

    async def update(self, full_text: str) -> None:
        if not self._throttle.should_commit(full_text):
            return
        try:
            await self._limiter.acquire()
            await self._send_draft(full_text)
            self._throttle.mark_committed(full_text)
            self._last_rendered = full_text
            # 首个 draft 预览已出现:停止 typing(draft 流式预览接管)
            if not self._first_token_sent:
                self._first_token_sent = True
                await self._stop_typing()
        except TelegramRetryAfter as e:
            await _sleep_retry_after(e)
        except Exception as e:
            log.warning("草稿更新失败(忽略,等待定稿)", 会话=self._chat_id,
                        错误=str(e)[:120])

    async def finalize(self, full_text: str) -> int | None:
        await self._stop_typing()
        expected_text = full_text.strip() or "(空回复)"
        text = merge_attachments(full_text, self.rich_attachments) or "(空回复)"
        text = clip(text)
        # 防弹化定稿:Rich 失败降级纯文本,限流退避,最终失败不抛出(避免冒泡 errors.py)
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                await self._limiter.acquire()
                msg = await self._bot(SendRichMessage(
                    chat_id=self._chat_id,
                    rich_message=to_rich_input(text),
                ))
                self._last_rendered = expected_text
                log.info("私聊回复已定稿", 会话=self._chat_id, 消息ID=msg.message_id,
                         长度=len(text))
                return msg.message_id
            except TelegramRetryAfter as e:
                last_exc = e
                await _sleep_retry_after(e)
            except TelegramBadRequest as e:
                msg_str = str(e)
                log.warning("私聊定稿Rich解析失败,降级纯文本", 会话=self._chat_id,
                            错误=msg_str[:120])
                try:
                    await self._limiter.acquire()
                    m = await self._bot.send_message(
                        self._chat_id, clip_markdown(text), parse_mode=None)
                    self._last_rendered = expected_text
                    return m.message_id
                except TelegramRetryAfter as re_:
                    last_exc = re_
                    await _sleep_retry_after(re_)
                    continue
                except Exception as e2:
                    last_exc = e2
                    continue
        log.error("私聊回复定稿最终失败(消息可能未送达)", 会话=self._chat_id,
                  错误=str(last_exc)[:160] if last_exc else "未知")
        return None

    async def fail(self, error_text: str) -> None:
        await self._stop_typing()
        try:
            await self._limiter.acquire()
            await self._bot(SendRichMessage(
                chat_id=self._chat_id,
                rich_message=to_rich_input(error_text),
            ))
        except Exception:
            # Rich 失败降级纯文本
            try:
                await self._limiter.acquire()
                await self._bot.send_message(
                    self._chat_id, clip_markdown(error_text), parse_mode=None)
            except Exception as e:
                log.error("发送错误提示失败", 会话=self._chat_id, 错误=str(e)[:120])

    @property
    def last_rendered_text(self) -> str:
        return self._last_rendered


class _TickLoopMixin:
    """统一轮询循环:内容更新与状态行渲染共用同一间隔门控。

    update() 仅暂存 _pending(非阻塞);真正发编辑的是后台 _tick_loop,
    每个 interval 做 1 次 tick,三选一(见 _tick_loop 文档):
      ① 有新内容 → 写正文 + 空行 + 状态行
      ② 内容静默 → idle no-op(不再编辑 —— 缓解 429 的核心)
      ③ 占位阶段 → 渲染纯状态行(默认「正在处理 ...」)
    状态行的切换由 set_status 驱动(而非光标闪烁)。
    间隔门控(_do_edit 咽喉点时间戳 + _ensure_interval_elapsed)保证「同一消息
    任意两次 edit 距离 ≥ interval」,杜绝多源并发与 finalize 触发的 429。
    """

    _interval_s: float
    _stop: asyncio.Event
    _tick_task: asyncio.Task | None
    _pending: str | None
    _committed: str
    _status: str
    _last_placeholder_render: str
    _last_edit_time: float

    def _tick_init(self, interval_s: float) -> None:
        self._interval_s = interval_s
        self._stop = asyncio.Event()
        self._tick_task = None
        self._pending = None
        self._committed = ""
        self._status = _STATUS_THINKING
        self._last_placeholder_render = ""
        self._last_edit_time = 0.0

    async def set_status(self, status: str) -> None:
        """设置状态行文案,并立即渲染当前状态(正文+状态行,或纯占位状态行)。

        立即渲染的理由:多轮工具调用中,Round1 正文已落地(_committed 非空),
        进入 idle 后若仅暂存,新状态(如「正在搜索」)要到下一段正文流式才会
        显示 —— 用户会在整个工具执行期看到旧状态。故 set_status 主动发一次编辑,
        使状态切换即时可见。编辑走 _raw_edit(受 interval 门控,429 安全)。
        """
        # 状态未变且尚无正文(占位阶段)→ 消息已显示该状态,跳过冗余编辑
        if status == self._status and not self._committed:
            return
        self._status = status
        if self._committed:
            rendered = self._render_body_with_status(self._committed)
        else:
            rendered = self._render_status_placeholder()
            # 同步去重缓存:占位状态主动渲染后,占位分支 tick 不应立即重复
            self._last_placeholder_render = rendered
        try:
            await self._raw_edit(rendered)
        except Exception as e:
            # 状态行仅作显示,任何渲染异常都不得中断 Agent 主循环
            log.warning("状态行渲染失败(忽略)", 错误=str(e)[:120])

    def _render_body_with_status(self, body: str) -> str:
        """正文 + 空行 + 斜体状态行(内容写入与 set_status 共用)。"""
        return body + "\n\n*" + self._status + "*"

    def _render_status_placeholder(self) -> str:
        """纯状态行(占位阶段)。"""
        return "*" + self._status + "*"

    async def _ensure_interval_elapsed(self) -> None:
        """距上次编辑不足 interval 时 sleep 补齐(咽喉点门控,防 429)。

        _do_edit 在 limiter.acquire() 前调用本方法(先补齐再取全局令牌,避免
        sleep 期间阻塞其它会话的发送)。tick 稳态下 elapsed 已 ≥ interval,直接
        返回零开销;仅在 finalize 紧随末次 tick 编辑时补齐,保证「同一消息任意两次
        edit 距离 ≥ interval」。首次编辑(_last_edit_time=0)不门控:send 与 edit 是
        不同速率桶,首条内容应立即可见。
        """
        if self._last_edit_time > 0:
            gap = self._interval_s - (time.monotonic() - self._last_edit_time)
            if gap > 0.001:
                await asyncio.sleep(gap)

    async def _tick_loop(self, do_edit: Any) -> None:
        """轮询循环:按 interval 驱动三种编辑。do_edit(text)->awaitable。

        分支:
        1. 有新内容(_pending != _committed):写入正文 + 空行 + 状态行(无光标)。
        2. 内容静默(_committed 非空、无新内容):idle no-op,不再编辑 ——
           这是缓解 429 的核心。状态行切换由 set_status 驱动,落到分支 1/3。
        3. 占位阶段(首条内容前):渲染纯状态行(默认「正在处理 ...」)。
        """
        try:
            while not self._stop.is_set():
                if self._pending is not None and self._pending != self._committed:
                    # ① 内容写入:正文 + 空行 + 状态行
                    text = self._pending
                    self._committed = self._pending
                    self._on_committed(self._pending)
                    self._pending = None
                    try:
                        await do_edit(self._render_body_with_status(text))
                    except Exception as e:
                        log.warning("轮询编辑失败(忽略)", 错误=str(e)[:120])
                elif self._committed:
                    # ② 内容静默(idle):不再编辑 —— 这是缓解 429 的核心。
                    # 状态行的切换由 set_status 驱动,会落到「有新内容」或
                    # 「占位阶段」分支;idle 期无需任何编辑。
                    pass
                else:
                    # ③ 占位阶段(首条内容前):渲染纯状态行;状态未变则不重复编辑
                    rendered = self._render_status_placeholder()
                    if rendered == self._last_placeholder_render:
                        pass  # 状态行未变,跳过编辑(避免 "not modified" 空转)
                    else:
                        self._last_placeholder_render = rendered
                        try:
                            await do_edit(rendered)
                        except Exception as e:
                            log.debug("占位状态行编辑失败(忽略)", 错误=str(e)[:120])
                # 等到下一个 tick(或停止信号)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise

    def _on_committed(self, text: str) -> None:
        """内容提交时的钩子(子类覆盖以记录 last_rendered)。"""
        pass

    async def _stop_tick(self) -> None:
        self._stop.set()
        if self._tick_task is not None and not self._tick_task.done():
            self._tick_task.cancel()
            try:
                await self._tick_task
            except (asyncio.CancelledError, Exception):
                pass
        self._tick_task = None


class EditRenderer(_TickLoopMixin):
    """群聊(成员):sendMessage 占位 → 统一轮询循环 editMessageText → 末次定稿。

    typing:群聊 bot 是成员,支持 sendChatAction。start 后全程发 typing 直到
    finalize/fail。状态行渲染由轮询循环在占位/内容写入期驱动(idle 不发编辑)。
    """

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        limiter: SendRateLimiter,
        throttle_ms: int = 3000,
        reply_to_message_id: int | None = None,
        placeholder: str = "*" + _STATUS_THINKING + "*",
        *,
        typing_refresh_s: float = 4.0,
        rich_attachments: RichAttachmentCollector | None = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._limiter = limiter
        self._reply_to = reply_to_message_id
        self._placeholder = placeholder
        self._message_id: int | None = None
        self._typing_refresh_s = typing_refresh_s
        self._typing_stop: asyncio.Event | None = None
        self._typing_task: asyncio.Task | None = None
        self._last_rendered = ""
        self.rich_attachments = rich_attachments
        self._tick_init(throttle_ms / 1000)

    def _on_committed(self, text: str) -> None:
        self._last_rendered = text

    async def _stop_typing(self) -> None:
        if self._typing_task is not None and not self._typing_task.done():
            if self._typing_stop is not None:
                self._typing_stop.set()
            self._typing_task.cancel()
            try:
                await self._typing_task
            except (asyncio.CancelledError, Exception):
                pass
        self._typing_task = None
        self._typing_stop = None

    async def start(self) -> None:
        from aiogram.types import ReplyParameters
        reply_params = ReplyParameters(message_id=self._reply_to) if self._reply_to else None
        while True:
            try:
                await self._limiter.acquire()
                msg = await self._bot(SendRichMessage(
                    chat_id=self._chat_id,
                    rich_message=to_rich_input(self._placeholder),
                    reply_parameters=reply_params,
                ))
                self._message_id = msg.message_id
                log.debug("占位消息已发送", 会话=self._chat_id, 消息ID=msg.message_id)
                break
            except TelegramRetryAfter as e:
                await _sleep_retry_after(e)
        # 群聊:全程 typing 直到 finalize
        self._typing_stop = asyncio.Event()
        self._typing_task = asyncio.create_task(
            _run_typing_loop(self._bot, self._chat_id, self._limiter,
                             self._typing_refresh_s, self._typing_stop))
        # 启动统一轮询循环(内容更新与状态行共用间隔,idle 不发编辑)
        self._tick_task = asyncio.create_task(
            self._tick_loop(self._do_edit))

    async def _do_edit(self, text: str, *, plain: bool = False) -> None:
        """单次编辑(供轮询循环与定稿共用)。

        含咽喉点间隔门控:acquire 后、HTTP 调用前记录时间戳,保证「同一消息
        任意两次 edit 距离 ≥ interval」。plain=True 时降级纯文本(Rich 解析失败)。
        """
        if self._message_id is None:
            return
        await self._ensure_interval_elapsed()
        await self._limiter.acquire()
        self._last_edit_time = time.monotonic()
        if plain:
            await self._bot.edit_message_text(
                clip_markdown(text),
                chat_id=self._chat_id,
                message_id=self._message_id,
                parse_mode=None,
            )
        else:
            await self._bot.edit_message_text(
                rich_message=to_rich_input(text),
                chat_id=self._chat_id,
                message_id=self._message_id,
            )

    async def _raw_edit(self, text: str, *, plain: bool = False) -> None:
        """带 RetryAfter/BadRequest 处理的编辑(轮询循环用)。"""
        while True:
            try:
                await self._do_edit(text, plain=plain)
                return
            except TelegramRetryAfter as e:
                await _sleep_retry_after(e)
            except TelegramBadRequest as e:
                if "message is not modified" in str(e):
                    return
                log.debug("轮询编辑BadRequest(忽略)", 错误=str(e)[:100])
                return

    async def update(self, full_text: str) -> None:
        """仅暂存最新文本;真正发编辑的是 _tick_loop。"""
        if self._message_id is None:
            return
        self._pending = full_text

    async def finalize(self, full_text: str) -> int | None:
        await self._stop_tick()
        await self._stop_typing()
        expected_text = full_text.strip() or "(空回复)"
        text = merge_attachments(full_text, self.rich_attachments).strip() or "(空回复)"
        if self._message_id is None:
            await self.start()
            await self._stop_tick()
            await self._stop_typing()
        # 防弹化末次编辑:Rich 失败降级纯文本,保证完整文本落地(杜绝截断)
        async def _edit_fn(rendered: str, *, plain: bool = False) -> None:
            await self._do_edit(rendered, plain=plain)
        ok = await _commit_final_edit(_edit_fn, text, chat_label=self._chat_id)
        if ok:
            self._last_rendered = expected_text
        log.info("编辑流回复已定稿", 会话=self._chat_id, 消息ID=self._message_id,
                 长度=len(text), 落地="成功" if ok else "失败")
        return self._message_id

    async def fail(self, error_text: str) -> None:
        await self._stop_tick()
        await self._stop_typing()
        try:
            if self._message_id is not None:
                async def _edit_fn(rendered: str, *, plain: bool = False) -> None:
                    await self._do_edit(rendered, plain=plain)
                await _commit_final_edit(_edit_fn, error_text, chat_label=self._chat_id)
            else:
                await self._limiter.acquire()
                await self._bot(SendRichMessage(
                    chat_id=self._chat_id,
                    rich_message=to_rich_input(error_text),
                ))
        except Exception:
            # Rich 失败降级纯文本
            try:
                await self._limiter.acquire()
                await self._bot.send_message(
                    self._chat_id, clip_markdown(error_text), parse_mode=None)
            except Exception as e:
                log.error("发送错误提示失败", 会话=self._chat_id, 错误=str(e)[:120])

    @property
    def message_id(self) -> int | None:
        return self._message_id

    @property
    def last_rendered_text(self) -> str:
        return self._last_rendered


class GuestRenderer(_TickLoopMixin):
    """Guest 模式:answerGuestQuery 一次应答入口(每次召唤仅此一次),
    返回 SentGuestMessage.inline_message_id,后续在其上统一轮询编辑。

    typing:Guest bot 非会话成员,**不支持** sendChatAction(仅 answerGuestQuery
    通道)。故用「状态行」(set_status 驱动,如「正在处理 ...」)作为唯一
    「工作中」信号 —— 由统一轮询循环在占位/内容写入期渲染(idle 不发编辑)。
    """

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        guest_query_id: str,
        limiter: SendRateLimiter,
        throttle_ms: int = 1000,
        *,
        rich_attachments: RichAttachmentCollector | None = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._guest_query_id = guest_query_id
        self._limiter = limiter
        self._inline_message_id: str | None = None
        self._pending_media: dict[str, Any] | None = None
        self._last_rendered = ""
        self.rich_attachments = rich_attachments
        self._tick_init(throttle_ms / 1000)

    def _on_committed(self, text: str) -> None:
        self._last_rendered = text

    def attach_pending(self, kind: str, url: str, note: str | None) -> None:
        """暂存一个待投递媒体(图片=photo / 语音=audio)。仅保留首个。"""
        if self._pending_media is None:
            self._pending_media = {"kind": kind, "url": url, "note": note or ""}

    def attach_rich_media(
        self,
        kind: str,
        url: str,
        *,
        label: str | None = None,
        note: str | None = None,
    ) -> RichAttachment | None:
        if self.rich_attachments is None:
            self.rich_attachments = RichAttachmentCollector()
        return self.rich_attachments.add(kind, url, label=label, note=note)

    async def start(self) -> None:
        try:
            await self._limiter.acquire()
            sent = await self._bot(AnswerGuestQuery(
                guest_query_id=self._guest_query_id,
                result=InlineQueryResultArticle(
                    id=self._guest_query_id[:60] or "answer",
                    title="回复",
                    input_message_content=InputRichMessageContent(
                        rich_message=to_rich_input("*" + _STATUS_THINKING + "*"),
                    ),
                ),
            ))
            self._inline_message_id = getattr(sent, "inline_message_id", None)
            log.info("Guest应答已发出", 会话=self._chat_id,
                     查询ID=self._guest_query_id,
                     内联消息ID=self._inline_message_id or "无")
        except Exception as e:
            log.error("answerGuestQuery失败", 查询ID=self._guest_query_id,
                      错误=str(e)[:200])
            raise
        # 启动统一轮询循环(内容更新 + 状态行渲染共用间隔)
        self._tick_task = asyncio.create_task(
            self._tick_loop(self._raw_edit))

    async def _do_edit(self, text: str, *, plain: bool = False) -> None:
        """单次编辑(供轮询循环与定稿共用)。含咽喉点间隔门控,详见基类。"""
        if self._inline_message_id is None:
            return
        await self._ensure_interval_elapsed()
        await self._limiter.acquire()
        self._last_edit_time = time.monotonic()
        if plain:
            await self._bot.edit_message_text(
                clip_markdown(text),
                inline_message_id=self._inline_message_id,
                parse_mode=None,
            )
        else:
            await self._bot.edit_message_text(
                rich_message=to_rich_input(text),
                inline_message_id=self._inline_message_id,
            )

    async def _raw_edit(self, text: str, *, plain: bool = False) -> None:
        """带 RetryAfter/BadRequest 处理的编辑(轮询循环用)。"""
        while True:
            try:
                await self._do_edit(text, plain=plain)
                return
            except TelegramRetryAfter as e:
                await _sleep_retry_after(e)
            except TelegramBadRequest as e:
                if "message is not modified" in str(e):
                    return
                log.debug("Guest轮询编辑BadRequest(忽略)", 错误=str(e)[:100])
                return

    async def update(self, full_text: str) -> None:
        """仅暂存最新文本;真正发编辑的是 _tick_loop。"""
        if self._inline_message_id is None:
            return
        self._pending = full_text

    async def _edit_media(self, media: Any) -> bool:
        if self._inline_message_id is None:
            return False
        try:
            await self._limiter.acquire()
            await self._bot(EditMessageMedia(
                inline_message_id=self._inline_message_id,
                media=media,
            ))
            return True
        except Exception as e:
            log.warning("Guest媒体编辑失败(降级为文本)", 会话=self._chat_id,
                        错误=str(e)[:160])
            return False

    async def finalize(self, full_text: str) -> int | None:
        await self._stop_tick()
        expected_text = full_text.strip() or "(空回复)"
        text = merge_attachments(full_text, self.rich_attachments).strip() or "(空回复)"
        pm = self._pending_media
        if pm and pm.get("kind") == "audio":
            self.attach_rich_media("audio", pm["url"], label="生成语音")
            text = merge_attachments(full_text, self.rich_attachments).strip() or text
        ok_final = False
        if pm and pm.get("kind") != "audio" and self._inline_message_id is not None:
            caption = clip_markdown(text)
            note = pm.get("note") or ""
            if note:
                caption = clip_markdown(caption + "\n" + note)
            kind, url = pm["kind"], pm["url"]
            media = self._build_media(kind, url, caption)
            ok_final = await self._edit_media(media)
            if not ok_final:
                link_line = f"\n\n✅ 已生成:[查看]({url})"
                async def _edit_fn(rendered: str, *, plain: bool = False) -> None:
                    await self._do_edit(rendered, plain=plain)
                ok_final = await _commit_final_edit(
                    _edit_fn, text + link_line, chat_label=self._chat_id)
        else:
            async def _edit_fn(rendered: str, *, plain: bool = False) -> None:
                await self._do_edit(rendered, plain=plain)
            ok_final = await _commit_final_edit(_edit_fn, text,
                                                chat_label=self._chat_id)
        if ok_final:
            self._last_rendered = expected_text
        log.info("Guest回复已定稿", 会话=self._chat_id,
                 内联消息ID=self._inline_message_id or "无",
                 形式=pm["kind"] if pm else "文本", 长度=len(full_text),
                 落地="成功" if ok_final else "失败")
        return None

    @staticmethod
    def _build_media(kind: str, url: str, caption: str) -> Any:
        # 媒体 caption 不支持 Rich Message,用纯文本(parse_mode=None)
        if kind == "photo":
            return InputMediaPhoto(media=url, caption=caption, parse_mode=None)
        if kind == "video":
            return InputMediaVideo(media=url, caption=caption, parse_mode=None)
        return InputMediaAudio(media=url, caption=caption, parse_mode=None)

    async def fail(self, error_text: str) -> None:
        await self._stop_tick()
        try:
            if self._inline_message_id is not None:
                async def _edit_fn(rendered: str, *, plain: bool = False) -> None:
                    await self._do_edit(rendered, plain=plain)
                await _commit_final_edit(_edit_fn, error_text,
                                         chat_label=self._chat_id)
        except Exception as e:
            log.error("Guest错误提示发送失败", 会话=self._chat_id, 错误=str(e)[:120])

    @property
    def last_rendered_text(self) -> str:
        return self._last_rendered
