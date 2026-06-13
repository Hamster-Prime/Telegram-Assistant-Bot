"""MiniMax 图片生成 —— /v1/image_generation(image-01 / image-01-live)。"""
from __future__ import annotations

from app.logging import get_logger
from app.minimax.client import MiniMaxClient

log = get_logger("minimax.image")


class ImageAPI:
    def __init__(self, client: MiniMaxClient, model: str = "image-01") -> None:
        self._client = client
        self._model = model

    async def generate(
        self,
        prompt: str,
        *,
        aspect_ratio: str = "1:1",
        n: int = 1,
        prompt_optimizer: bool = True,
    ) -> list[str]:
        """文生图,返回图片 URL 列表(24h 有效,需即时下载转存)。"""
        if len(prompt) > 1500:
            prompt = prompt[:1500]
            log.warning("图片提示词超长已截断", 截断后长度=len(prompt))
        n = max(1, min(9, n))

        payload = {
            "model": self._model,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "n": n,
            "prompt_optimizer": prompt_optimizer,
            "response_format": "url",
        }
        log.info("图片生成请求", 模型=self._model, 比例=aspect_ratio, 数量=n,
                 提示词=prompt[:80])
        data = await self._client.post("/image_generation", payload)
        urls = (data.get("data") or {}).get("image_urls") or []
        log.info("图片生成完成", 数量=len(urls))
        return list(urls)
