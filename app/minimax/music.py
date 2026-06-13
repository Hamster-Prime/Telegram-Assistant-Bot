"""MiniMax 音乐生成 —— /v1/music_generation(music-2.6)。"""
from __future__ import annotations

from app.logging import get_logger
from app.minimax.client import MiniMaxClient
from app.utils.hexaudio import decode_hex_audio

log = get_logger("minimax.music")


class MusicAPI:
    def __init__(self, client: MiniMaxClient, model: str = "music-2.6") -> None:
        self._client = client
        self._model = model

    async def generate(
        self,
        prompt: str,
        *,
        lyrics: str | None = None,
        is_instrumental: bool = False,
        audio_format: str = "mp3",
        sample_rate: int = 44100,
    ) -> tuple[bytes | None, str | None]:
        """生成音乐。返回 (音频字节 | None, 音频URL | None)。

        非纯音乐必须提供 lyrics(≤3500 字符)。
        """
        if len(prompt) > 2000:
            prompt = prompt[:2000]
            log.warning("音乐提示词超长已截断", 截断后长度=len(prompt))
        if lyrics and len(lyrics) > 3500:
            lyrics = lyrics[:3500]
            log.warning("歌词超长已截断", 截断后长度=len(lyrics))
        if not is_instrumental and not lyrics:
            lyrics = prompt  # 兜底:非纯音乐但未给歌词,用描述充当

        payload: dict = {
            "model": self._model,
            "prompt": prompt,
            "is_instrumental": is_instrumental,
            "audio_setting": {
                "sample_rate": sample_rate, "bitrate": 256000, "format": audio_format,
            },
            "output_format": "url",
        }
        if lyrics:
            payload["lyrics"] = lyrics
        log.info("音乐生成请求", 模型=self._model, 纯音乐=is_instrumental,
                 歌词长度=len(lyrics or ""), 提示词=prompt[:80])
        data = await self._client.post("/music_generation", payload)
        audio_field = (data.get("data") or {}).get("audio", "")
        if audio_field.startswith("http"):
            log.info("音乐生成完成", 形式="URL")
            return None, audio_field
        audio_bytes = await decode_hex_audio(audio_field)
        log.info("音乐生成完成", 形式="hex已解码",
                 大小KB=round(len(audio_bytes) / 1024, 1))
        return audio_bytes, None
