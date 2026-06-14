"""工具 schema 定义 + 分发表(plan §13)。

同步工具(搜索/抓取/时间/记忆):Agent 主循环直接 await 结果回灌。
异步生成工具(视频/音乐):交 GenWorkerPool,立即回灌"已入队"。
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from app.logging import get_logger
from app.utils.clock import format_now

log = get_logger("core.tools")

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "根据文字描述生成图片(同步,数秒内完成)。"
                "当用户消息中包含图片或回复了图片时,会自动进入图生图模式(以该图片人物为主体重新生成)。"
                "可选画风预设(image-01-live 模型):漫画/元气/中世纪/水彩。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "图片描述(中英文皆可)"},
                    "aspect_ratio": {"type": "string", "enum": ["1:1", "16:9", "9:16", "4:3", "3:4"],
                                     "description": "宽高比,默认 1:1"},
                    "n": {"type": "integer", "minimum": 1, "maximum": 9,
                          "description": "生成张数,默认 1"},
                    "style_type": {"type": "string",
                                   "enum": ["漫画", "元气", "中世纪", "水彩"],
                                   "description": "画风预设(可选,使用 image-01-live 模型)"},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_video",
            "description": (
                "根据文字描述生成视频(异步后台任务,需数分钟)。"
                "当用户消息含图片时自动进入图生视频模式:"
                "1张图→图生视频(首帧);2张图→首尾帧生成;设为 subject_reference→主体参考(S2V)。"
                "调用后告知用户已开始,完成后另行发送。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "视频内容描述"},
                    "duration": {"type": "integer", "enum": [6, 10], "description": "时长秒数,默认 6"},
                    "resolution": {"type": "string", "enum": ["768P", "1080P"], "description": "分辨率,默认 768P"},
                    "reference_mode": {"type": "string",
                                       "enum": ["first_frame", "subject_reference"],
                                       "description": "图片参考模式(仅消息含图片时有效)。"
                                                      "first_frame=首帧图生视频(默认),"
                                                      "subject_reference=主体参考(保持人物面部)"},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synthesize_speech",
            "description": (
                "把文本合成为语音并发送给用户(同步)。"
                "可指定音色ID(系统音色或复刻/设计的自定义音色)、情绪、语言增强。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要朗读的文本,≤10000字"},
                    "voice_id": {"type": "string", "description": "音色ID,默认男声青涩"},
                    "emotion": {"type": "string",
                                "enum": ["happy", "sad", "angry", "fearful", "disgusted", "surprised", "neutral"],
                                "description": "情绪,可选"},
                    "language_boost": {"type": "string",
                                       "description": "语言增强,如 Chinese / English / Japanese / auto"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_music",
            "description": "根据描述(可含歌词)生成歌曲/音乐(异步后台任务;调用后告知用户已开始)",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "音乐风格/主题描述"},
                    "lyrics": {"type": "string", "description": "歌词(非纯音乐时建议提供)"},
                    "is_instrumental": {"type": "boolean", "description": "是否纯音乐,默认否"},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clone_voice",
            "description": (
                "音色复刻:根据用户发送或回复的语音/音频文件,克隆该音色。"
                "调用前用户必须回复一条语音/音频消息(或直接发送)。"
                "复刻得到的音色7天内需正式用于语音合成才会永久保留。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "voice_id": {"type": "string",
                                 "description": "自定义音色ID:首字符必须为字母,允许数字字母-_,"
                                                "长度8-256,不可与已有ID重复"},
                    "preview_text": {"type": "string",
                                     "description": "试听文本(可选),提供后返回试听音频"},
                },
                "required": ["voice_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "design_voice",
            "description": (
                "音色设计:根据文字描述生成一个全新的个性化音色。"
                "如'低沉磁性的男声播音员'。返回音色ID和试听音频。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "音色描述(如'温柔年轻女声、语速轻快')"},
                    "preview_text": {"type": "string", "description": "试听文本,≤500字"},
                    "voice_id": {"type": "string", "description": "自定义音色ID(可选,不传则自动生成)"},
                },
                "required": ["prompt", "preview_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_voices",
            "description": "查询当前可用的音色ID列表(系统音色/复刻音色/设计音色)",
            "parameters": {
                "type": "object",
                "properties": {
                    "voice_type": {"type": "string",
                                   "enum": ["system", "voice_cloning", "voice_generation", "all"],
                                   "description": "音色类型,默认 all(全部)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "联网搜索事实和最新信息。用户明确说“搜一下/查一下/帮我搜/最新/最近/新闻/动态”"
                "或询问可能变化的现实信息时必须调用本工具。不要只承诺搜索;调用后基于结果回答。"
                "返回标题/链接/摘要列表;需要详细正文时再用 web_fetch 抓取具体链接"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "count": {"type": "integer", "minimum": 1, "maximum": 10,
                              "description": "结果条数,默认 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "抓取指定 URL 的网页正文(markdown)",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整 URL"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前精确时间(默认上海时区,可指定其他 IANA 时区)",
            "parameters": {
                "type": "object",
                "properties": {
                    "tz": {"type": "string", "description": "IANA 时区名,如 Asia/Shanghai、America/New_York"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "把重要信息存入长期记忆(用户偏好、事实、约定等)",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要记住的内容,一句话"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "在长期记忆中检索相关内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词"},
                },
                "required": ["query"],
            },
        },
    },
]

ToolFunc = Callable[[dict[str, Any]], Awaitable[str]]


class ToolDispatcher:
    """工具名 → 执行函数注册表。每次请求构造一个(绑定 user/chat 上下文)。"""

    def __init__(self) -> None:
        self._funcs: dict[str, ToolFunc] = {}
        # 默认注册纯本地工具
        self.register("get_current_time", self._get_current_time)

    def register(self, name: str, func: ToolFunc) -> None:
        self._funcs[name] = func

    async def dispatch(self, name: str, raw_arguments: str) -> str:
        """执行工具,返回回灌给模型的字符串结果。异常转为错误文本(模型可读)。"""
        func = self._funcs.get(name)
        if func is None:
            log.warning("未注册的工具被调用", 工具=name)
            return f"错误:工具 {name} 不可用"
        try:
            args = json.loads(raw_arguments) if raw_arguments.strip() else {}
        except json.JSONDecodeError as e:
            log.warning("工具参数JSON解析失败", 工具=name, 原始参数=raw_arguments[:200],
                        错误=str(e))
            return f"错误:参数不是合法 JSON({e})"
        import time as _t
        t0 = _t.monotonic()
        try:
            result = await func(args)
            log.info("工具执行成功", 工具=name, 参数=json.dumps(args, ensure_ascii=False)[:200],
                     耗时毫秒=round((_t.monotonic() - t0) * 1000),
                     结果预览=result[:120])
            return result
        except Exception as e:
            log.error("工具执行失败", 工具=name,
                      参数=json.dumps(args, ensure_ascii=False)[:200],
                      异常类型=type(e).__name__, 详情=str(e)[:300],
                      耗时毫秒=round((_t.monotonic() - t0) * 1000))
            # 把用户可读的错误信息回灌给模型,让它向用户解释
            user_msg = getattr(e, "user_message", None)
            text = user_msg() if callable(user_msg) else str(e)
            return f"工具执行失败:{text}"

    @staticmethod
    async def _get_current_time(args: dict[str, Any]) -> str:
        tz = args.get("tz") or "Asia/Shanghai"
        try:
            return f"当前时间:{format_now(tz)}"
        except Exception:
            return f"错误:无效时区 {tz},请用 IANA 名称如 Asia/Shanghai"
