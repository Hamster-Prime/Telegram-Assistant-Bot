"""MiniMax 音色管理 —— 音色复刻 / 音色设计 / 查询音色 / 删除音色。

- POST /voice_clone    音色快速复刻(上传音频 file_id + 自定义 voice_id)
- POST /voice_design   音色设计(文本描述 → 生成 voice_id + 试听音频)
- POST /get_voice      查询可用音色(system / voice_cloning / voice_generation / all)
- POST /delete_voice   删除指定音色

注意:
- 复刻/设计得到的为临时音色,7 天内未在 T2A 正式调用会被系统删除。
- 调用复刻/设计接口本身不收费,首次用于语音合成时才计费。
"""
from __future__ import annotations

from typing import Any

from app.logging import get_logger
from app.minimax.client import MiniMaxClient
from app.utils.hexaudio import decode_hex_audio

log = get_logger("minimax.voice")


class VoiceAPI:
    def __init__(self, client: MiniMaxClient) -> None:
        self._client = client

    async def clone(
        self,
        file_id: int,
        voice_id: str,
        *,
        clone_prompt: dict[str, Any] | None = None,
        text: str | None = None,
        model: str | None = None,
        language_boost: str | None = None,
        need_noise_reduction: bool = False,
        need_volume_normalization: bool = False,
    ) -> dict[str, Any]:
        """音色快速复刻。返回包含 demo_audio(试听URL,若提供text)的字典。

        file_id:待复刻音频的 file_id(经 FilesAPI 上传获得)。
        voice_id:自定义音色 ID(8-256 字符,首字符字母,允许数字字母-_)。
        clone_prompt:{prompt_audio: int, prompt_text: str} 可选,增强相似度。
        text:试听文本(≤1000 字符),提供时返回 demo_audio 试听链接。
        model:试听合成模型(提供 text 时必填)。
        """
        payload: dict[str, Any] = {
            "file_id": file_id,
            "voice_id": voice_id,
            "need_noise_reduction": need_noise_reduction,
            "need_volume_normalization": need_volume_normalization,
        }
        if clone_prompt:
            payload["clone_prompt"] = clone_prompt
        if text:
            payload["text"] = text[:1000]
            payload["model"] = model or "speech-2.8-hd"
        if language_boost:
            payload["language_boost"] = language_boost

        log.info("音色复刻请求", 音色ID=voice_id, 文件ID=file_id,
                 有试听=bool(text), 模型=payload.get("model"))
        data = await self._client.post("/voice_clone", payload)
        demo = data.get("demo_audio") or ""
        log.info("音色复刻完成", 音色ID=voice_id, 有试听=bool(demo))
        return {"voice_id": voice_id, "demo_audio": demo,
                "extra_info": data.get("extra_info") or {}}

    async def design(
        self,
        prompt: str,
        preview_text: str,
        *,
        voice_id: str | None = None,
    ) -> tuple[str, bytes | None]:
        """音色设计。返回 (voice_id, 试听音频字节 | None)。

        prompt:音色描述(如"低沉磁性男声")。
        preview_text:试听文本(≤500 字符,试听按字符收费)。
        voice_id:可选自定义 ID,不传则自动生成。
        """
        payload: dict[str, Any] = {
            "prompt": prompt,
            "preview_text": preview_text[:500],
        }
        if voice_id:
            payload["voice_id"] = voice_id
        log.info("音色设计请求", 描述=prompt[:80], 试听长度=len(preview_text),
                 自定义ID=bool(voice_id))
        data = await self._client.post("/voice_design", payload)
        new_voice_id = data.get("voice_id") or ""
        trial_hex = data.get("trial_audio") or ""
        trial_bytes: bytes | None = None
        if trial_hex:
            trial_bytes = await decode_hex_audio(trial_hex)
            log.info("音色设计完成", 音色ID=new_voice_id,
                     试听KB=round(len(trial_bytes) / 1024, 1))
        else:
            log.info("音色设计完成", 音色ID=new_voice_id, 试听="无")
        return new_voice_id, trial_bytes

    async def list_voices(self, voice_type: str = "all") -> dict[str, Any]:
        """查询可用音色。voice_type: system / voice_cloning / voice_generation / all。

        返回原始结构:{system_voice: [...], voice_cloning: [...], voice_generation: [...]}。
        """
        payload = {"voice_type": voice_type}
        log.info("查询可用音色", 类型=voice_type)
        data = await self._client.post("/get_voice", payload)
        counts = {k: len(v) for k, v in data.items()
                  if isinstance(v, list)}
        log.info("查询可用音色完成", 类型=voice_type, 各类数量=counts)
        return data

    async def delete(self, voice_id: str) -> bool:
        """删除指定音色。返回是否成功。"""
        payload = {"voice_id": voice_id}
        log.info("删除音色", 音色ID=voice_id)
        await self._client.post("/delete_voice", payload)
        log.info("音色已删除", 音色ID=voice_id)
        return True
