"""MiniMax 图片生成 —— /v1/image_generation(image-01 / image-01-live)。

支持:
- 文生图:仅 prompt
- 图生图:subject_reference(人物主体参考)
- image-01-live 画风:style 参数(漫画/元气/中世纪/水彩)
"""
from __future__ import annotations

from typing import Any

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
        subject_reference: list[dict[str, Any]] | None = None,
        model: str | None = None,
        style: dict[str, Any] | None = None,
    ) -> list[str]:
        """图片生成,返回图片 URL 列表(24h 有效,需即时下载转存)。

        - 纯文生图:仅传 prompt。
        - 图生图:传 subject_reference=[{"type": "character", "image_file": <url|dataurl>}],
          以参考图中人物为主体生成。
        - 画风:仅 model="image-01-live" 时 style 生效,
          style={"style_type": "漫画", "style_weight": 0.8}。
        """
        if len(prompt) > 1500:
            prompt = prompt[:1500]
            log.warning("图片提示词超长已截断", 截断后长度=len(prompt))
        n = max(1, min(9, n))

        use_model = model or self._model
        payload: dict[str, Any] = {
            "model": use_model,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "n": n,
            "prompt_optimizer": prompt_optimizer,
            "response_format": "url",
        }
        if subject_reference:
            payload["subject_reference"] = subject_reference
        if style:
            payload["style"] = style

        log.info("图片生成请求", 模型=use_model, 比例=aspect_ratio, 数量=n,
                 图生图=bool(subject_reference), 画风=bool(style),
                 提示词=prompt[:80])
        data = await self._client.post("/image_generation", payload)
        urls = (data.get("data") or {}).get("image_urls") or []
        log.info("图片生成完成", 数量=len(urls))
        return list(urls)
