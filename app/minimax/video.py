"""MiniMax 视频生成 —— /v1/video_generation(MiniMax-Hailuo-2.3,异步任务)。"""
from __future__ import annotations

from app.logging import get_logger
from app.minimax.client import MiniMaxClient

log = get_logger("minimax.video")


class VideoAPI:
    def __init__(self, client: MiniMaxClient, model: str = "MiniMax-Hailuo-2.3") -> None:
        self._client = client
        self._model = model

    async def create_task(
        self,
        prompt: str,
        *,
        duration: int = 6,
        resolution: str = "768P",
        prompt_optimizer: bool = True,
        callback_url: str | None = None,
    ) -> str:
        """建视频生成任务,返回 task_id。"""
        duration = 10 if duration >= 10 else 6
        payload: dict = {
            "model": self._model,
            "prompt": prompt,
            "duration": duration,
            "resolution": resolution,
            "prompt_optimizer": prompt_optimizer,
        }
        if callback_url:
            payload["callback_url"] = callback_url
        log.info("视频生成任务创建", 模型=self._model, 时长秒=duration,
                 分辨率=resolution, 回调=bool(callback_url), 提示词=prompt[:80])
        data = await self._client.post("/video_generation", payload)
        task_id = str(data.get("task_id", ""))
        log.info("视频生成任务已受理", 任务ID=task_id)
        return task_id

    async def query_task(self, task_id: str) -> tuple[str, str | None]:
        """查询任务状态。返回 (status, file_id);status ∈ processing/success/failed 等。"""
        data = await self._client.get("/query/video_generation",
                                      params={"task_id": task_id})
        status = str(data.get("status", "")).lower()
        file_id = data.get("file_id")
        log.info("视频任务状态查询", 任务ID=task_id, 状态=status,
                 文件ID=file_id or "无")
        return status, (str(file_id) if file_id else None)
