"""MiniMax 语音合成 —— /v1/t2a_v2(speech-2.8-hd / turbo)。"""
from __future__ import annotations

from typing import Any

from app.logging import get_logger
from app.minimax.client import MiniMaxClient
from app.utils.hexaudio import decode_hex_audio

log = get_logger("minimax.tts")


class TTSAPI:
    def __init__(self, client: MiniMaxClient, model: str = "speech-2.8-hd") -> None:
        self._client = client
        self._model = model

    async def synthesize(
        self,
        text: str,
        *,
        voice_id: str = "male-qn-qingse",
        speed: float = 1.0,
        vol: float = 1.0,
        pitch: int = 0,
        emotion: str | None = None,
        language_boost: str | None = None,
        audio_format: str = "mp3",
        sample_rate: int = 32000,
        output_format: str = "url",
    ) -> tuple[bytes | None, str | None, int]:
        """文本转语音。返回 (音频字节 | None, 音频URL | None, 时长毫秒)。

        默认 output_format=url 省去 hex 解码;url 模式下返回 (None, url, ms)。
        language_boost:增强小语种/方言识别,如 "Chinese"、"English"、"auto"。
        """
        if len(text) > 10000:
            text = text[:10000]
            log.warning("TTS文本超长已截断", 截断后长度=len(text))

        voice_setting: dict[str, Any] = {
            "voice_id": voice_id, "speed": speed, "vol": vol, "pitch": pitch,
        }
        if emotion:
            voice_setting["emotion"] = emotion

        payload: dict[str, Any] = {
            "model": self._model,
            "text": text,
            "voice_setting": voice_setting,
            "audio_setting": {
                "sample_rate": sample_rate, "bitrate": 128000,
                "format": audio_format, "channel": 1,
            },
            "output_format": output_format,
        }
        if language_boost:
            payload["language_boost"] = language_boost
        log.info("语音合成请求", 模型=self._model, 文本长度=len(text),
                 音色=voice_id, 情绪=emotion or "默认",
                 语言增强=language_boost or "默认", 输出格式=output_format)
        data = await self._client.post("/t2a_v2", payload)

        audio_field = (data.get("data") or {}).get("audio", "")
        extra = data.get("extra_info") or {}
        duration_ms = int(extra.get("audio_length", 0))

        if output_format == "url" or audio_field.startswith("http"):
            log.info("语音合成完成", 时长毫秒=duration_ms, 形式="URL")
            return None, audio_field, duration_ms

        audio_bytes = await decode_hex_audio(audio_field)
        log.info("语音合成完成", 时长毫秒=duration_ms, 形式="hex已解码",
                 大小KB=round(len(audio_bytes) / 1024, 1))
        return audio_bytes, None, duration_ms
