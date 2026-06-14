"""入站媒体解析 —— Telegram 媒体 → M3 多模态 content 块(plan §12)。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aiogram.types import ExternalReplyInfo, Message, PhotoSize

from app.logging import get_logger
from app.services import Services
from app.utils.tg_files import (
    IMAGE_INLINE_LIMIT,
    download_file,
    guess_mime,
    to_data_url,
)

log = get_logger("handlers.media")

_DOC_EXTS = {"pdf", "docx", "txt"}
_IMG_EXTS = {"jpg", "jpeg", "png", "gif", "webp"}
_AUDIO_EXTS = {"mp3", "m4a", "wav"}


@dataclass
class ReferenceAssets:
    """当前消息及被回复消息中提取的参考素材,供生成工具使用。

    - images: 图片 data URL 列表(供图生图/图生视频/首尾帧/主体参考)
    - audio:  音频原始字节(供音色复刻)
    - audio_filename: 音频文件名
    """
    images: list[str] = field(default_factory=list)
    audio: bytes | None = None
    audio_filename: str = ""

    @property
    def has_any(self) -> bool:
        return bool(self.images) or self.audio is not None


async def _append_image(
    svc: Services,
    blocks: list[dict[str, Any]],
    file_id: str,
    mime: str,
    chat_id: int,
    *,
    file_size: int | None = None,
    label: str = "图片",
) -> None:
    if (file_size or 0) > IMAGE_INLINE_LIMIT:
        raise ValueError(f"{label}超过 10MB,无法处理")
    data, _path = await download_file(svc.bot, file_id)
    url = await to_data_url(data, mime)
    blocks.append({"type": "image_url", "image_url": {"url": url}})
    log.info("入站图片已转base64", 会话=chat_id, 类型=label,
             大小KB=round(len(data) / 1024, 1))


def _thumbnail_mime(thumbnail: PhotoSize) -> str:
    return guess_mime(thumbnail.file_id, "image/jpeg")


async def build_content(
    svc: Services,
    message: Message | ExternalReplyInfo,
) -> tuple[Any | None, str]:
    """把 Telegram 消息转为 M3 content(字符串或多模态块列表)。

    返回 (content, 纯文本提要 query_text)。content 为 None 表示该消息应被忽略。
    """
    raw_text = getattr(message, "text", None)
    raw_caption = getattr(message, "caption", None)
    text = raw_text or raw_caption or ""
    chat_id = getattr(getattr(message, "chat", None), "id", 0)
    photo = getattr(message, "photo", None)
    video = getattr(message, "video", None)
    document = getattr(message, "document", None)
    sticker = getattr(message, "sticker", None)
    animation = getattr(message, "animation", None)

    # 纯文本
    if raw_text and not (
        photo or video or document or sticker or animation
    ):
        return text, text

    # Telegram video 按需求直接忽略,不下载也不传给模型。
    if video and not (photo or document or sticker or animation or text):
        return None, ""

    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})

    try:
        # 图片
        if photo:
            photo_size = photo[-1]  # 最大尺寸
            await _append_image(
                svc, blocks, photo_size.file_id, "image/jpeg", chat_id,
                file_size=photo_size.file_size, label="图片",
            )

        # 贴纸:普通贴纸按图片处理;动态贴纸/GIF 贴纸使用缩略图。
        if sticker:
            if sticker.is_animated or sticker.is_video:
                if sticker.thumbnail is None:
                    raise ValueError("动态贴纸缺少缩略图")
                await _append_image(
                    svc, blocks, sticker.thumbnail.file_id,
                    _thumbnail_mime(sticker.thumbnail), chat_id,
                    file_size=sticker.thumbnail.file_size, label="贴纸缩略图",
                )
            else:
                await _append_image(
                    svc, blocks, sticker.file_id, "image/webp", chat_id,
                    file_size=sticker.file_size, label="贴纸",
                )

        # GIF/动画消息:使用缩略图作为图片传给模型。
        if animation:
            if animation.thumbnail is None:
                raise ValueError("GIF 缺少缩略图")
            await _append_image(
                svc, blocks, animation.thumbnail.file_id,
                _thumbnail_mime(animation.thumbnail), chat_id,
                file_size=animation.thumbnail.file_size, label="GIF缩略图",
            )

        # 文档
        if document:
            doc = document
            name = doc.file_name or "file"
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            data, path = await download_file(svc.bot, doc.file_id)
            if ext in _IMG_EXTS:
                url = await to_data_url(data, guess_mime(name, "image/png"))
                blocks.append({"type": "image_url", "image_url": {"url": url}})
            elif ext in _DOC_EXTS:
                file_id = await svc.files_api.upload(data, name)
                blocks.append({"type": "text",
                               "text": f"(用户上传了文档 {name},文件引用 mm_file://{file_id})"})
                log.info("入站文档已转存FilesAPI", 会话=chat_id,
                         文件名=name, 文件ID=file_id)
            else:
                blocks.append({"type": "text",
                               "text": f"(用户上传了暂不支持解析的文件:{name})"})
    except ValueError as e:
        blocks.append({"type": "text", "text": f"(媒体处理失败:{e})"})
        log.warning("入站媒体处理失败", 会话=chat_id, 原因=str(e))

    if not blocks:
        return text or "(空消息)", text
    if len(blocks) == 1 and blocks[0].get("type") == "text":
        return blocks[0]["text"], blocks[0]["text"]
    return blocks, text or "(多媒体消息)"


async def _extract_image_refs(
    svc: Services, message: Message | ExternalReplyInfo,
    refs: ReferenceAssets, chat_id: int,
) -> None:
    """从单条消息提取图片参考(图片消息/图片文档)。追加到 refs.images。"""
    photo = getattr(message, "photo", None)
    document = getattr(message, "document", None)

    if photo:
        photo_size = photo[-1]
        if (photo_size.file_size or 0) <= IMAGE_INLINE_LIMIT:
            try:
                data, _ = await download_file(svc.bot, photo_size.file_id)
                url = await to_data_url(data, "image/jpeg")
                refs.images.append(url)
                log.info("参考图片已提取", 会话=chat_id, 来源="图片消息",
                         大小KB=round(len(data) / 1024, 1))
            except (ValueError, Exception) as e:
                log.warning("参考图片提取失败", 会话=chat_id, 原因=str(e)[:160])

    if document:
        name = document.file_name or "file"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in _IMG_EXTS and (document.file_size or 0) <= IMAGE_INLINE_LIMIT:
            try:
                data, _ = await download_file(svc.bot, document.file_id)
                url = await to_data_url(data, guess_mime(name, "image/png"))
                refs.images.append(url)
                log.info("参考图片已提取", 会话=chat_id, 来源="图片文档",
                         文件名=name, 大小KB=round(len(data) / 1024, 1))
            except (ValueError, Exception) as e:
                log.warning("参考图片文档提取失败", 会话=chat_id, 原因=str(e)[:160])


async def _extract_audio_ref(
    svc: Services, message: Message | ExternalReplyInfo,
    refs: ReferenceAssets, chat_id: int,
) -> None:
    """从单条消息提取音频参考(语音/音频消息/音频文档)。写入 refs.audio。"""
    if refs.audio is not None:
        return  # 仅保留第一个音频

    voice = getattr(message, "voice", None)
    audio = getattr(message, "audio", None)
    document = getattr(message, "document", None)

    target = None
    filename = ""
    if voice:
        target = voice.file_id
        filename = "voice.ogg"
    elif audio:
        target = audio.file_id
        filename = audio.file_name or "audio.mp3"
    elif document:
        name = document.file_name or ""
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in _AUDIO_EXTS:
            target = document.file_id
            filename = name

    if target:
        try:
            data, _ = await download_file(svc.bot, target)
            refs.audio = data
            refs.audio_filename = filename
            log.info("参考音频已提取", 会话=chat_id, 来源=filename,
                     大小KB=round(len(data) / 1024, 1))
        except (ValueError, Exception) as e:
            log.warning("参考音频提取失败", 会话=chat_id, 原因=str(e)[:160])


async def extract_references(
    svc: Services, message: Message,
) -> ReferenceAssets:
    """从当前消息及被回复消息中提取参考素材(图片/音频)。

    扫描范围:① 当前消息本身;② 被回复的消息(reply_to_message)。
    用于:
    - 图片参考 → 图生图 / 图生视频 / 首尾帧 / 主体参考
    - 音频参考 → 音色复刻(回复一条语音/音频 + "/clone 音色名")
    """
    refs = ReferenceAssets()
    chat_id = message.chat.id

    # ① 当前消息
    await _extract_image_refs(svc, message, refs, chat_id)
    await _extract_audio_ref(svc, message, refs, chat_id)

    # ② 被回复的消息
    reply_msg = message.reply_to_message or getattr(message, "external_reply", None)
    if reply_msg is not None:
        await _extract_image_refs(svc, reply_msg, refs, chat_id)
        await _extract_audio_ref(svc, reply_msg, refs, chat_id)

    if refs.has_any:
        log.info("参考素材汇总", 会话=chat_id,
                 图片数=len(refs.images), 有音频=refs.audio is not None)
    return refs


async def build_group_content(
    svc: Services, messages: list[Message],
) -> tuple[Any, str, list[str]]:
    """把相册(多条 Message)合并为多模态 content。

    返回 (content, query_text, image_urls):
    - content:多模态块列表(全部图片 + 合并 caption 文本)
    - query_text:合并的纯文本(取所有 caption 拼接)
    - image_urls:全部图片的 data URL(供参考素材用)

    messages 应已按 message_id 排序(由 MediaGroupBuffer 保证)。
    """
    blocks: list[dict[str, Any]] = []
    captions: list[str] = []
    image_urls: list[str] = []
    chat_id = messages[0].chat.id if messages else 0

    for msg in messages:
        if msg.caption:
            captions.append(msg.caption)
        if msg.photo:
            photo_size = msg.photo[-1]
            if (photo_size.file_size or 0) <= IMAGE_INLINE_LIMIT:
                try:
                    data, _ = await download_file(svc.bot, photo_size.file_id)
                    url = await to_data_url(data, "image/jpeg")
                    blocks.append({"type": "image_url", "image_url": {"url": url}})
                    image_urls.append(url)
                except (ValueError, Exception) as e:
                    log.warning("相册图片处理失败", 会话=chat_id, 原因=str(e)[:160])

    merged_text = "\n".join(captions)
    if merged_text:
        blocks.insert(0, {"type": "text", "text": merged_text})

    query_text = merged_text or "(相册消息)"
    log.info("相册内容已构建", 会话=chat_id, 图片数=len(image_urls),
             有文本=bool(merged_text))
    return blocks, query_text, image_urls
