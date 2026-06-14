"""MiniMax 视频生成 —— /v1/video_generation(MiniMax-Hailuo-2.3,异步任务)。

单端点多模式:
- 文生视频(T2V):model=MiniMax-Hailuo-2.3,仅 prompt
- 图生视频(I2V):model=MiniMax-Hailuo-2.3,prompt + first_frame_image
- 首尾帧(FL2V):model=MiniMax-Hailuo-02,first_frame_image + last_frame_image
- 主体参考(S2V):model=S2V-01,subject_reference=[{type, image:[url]}]

三种参考模式共用同一 /video_generation 端点,通过传入参数自动选择模型。
"""
from __future__ import annotations

from typing import Any

from app.logging import get_logger
from app.minimax.client import MiniMaxClient

log = get_logger("minimax.video")

# 各模式默认模型
_FL2V_MODEL = "MiniMax-Hailuo-02"
_S2V_MODEL = "S2V-01"


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
        first_frame_image: str | None = None,
        last_frame_image: str | None = None,
        subject_reference: list[dict[str, Any]] | None = None,
    ) -> str:
        """建视频生成任务,返回 task_id。

        按传入的参考图自动选择模型与模式:
        - subject_reference 非空 → S2V(model=S2V-01)
        - first_frame + last_frame 非空 → FL2V(model=MiniMax-Hailuo-02)
        - first_frame 非空 → I2V(默认 model)
        - 均空 → T2V(默认 model)
        """
        duration = 10 if duration >= 10 else 6

        # 按参考模式选模型
        model = self._model
        if subject_reference:
            model = _S2V_MODEL
        elif last_frame_image:
            model = _FL2V_MODEL
        # I2V / T2V 用默认 model

        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "prompt_optimizer": prompt_optimizer,
        }
        # 参考模式特有参数
        if subject_reference:
            payload["subject_reference"] = subject_reference
        else:
            if first_frame_image:
                payload["first_frame_image"] = first_frame_image
            if last_frame_image:
                payload["last_frame_image"] = last_frame_image
            # T2V/I2V/FL2V 支持 duration 与 resolution
            payload["duration"] = duration
            payload["resolution"] = resolution
        if callback_url:
            payload["callback_url"] = callback_url

        mode = ("S2V" if subject_reference
                else "FL2V" if last_frame_image
                else "I2V" if first_frame_image
                else "T2V")
        log.info("视频生成任务创建", 模式=mode, 模型=model, 时长秒=duration,
                 分辨率=resolution, 回调=bool(callback_url), 提示词=prompt[:80])
        try:
            data = await self._client.post("/video_generation", payload)
        except Exception as e:
            # 回调 URL 验证失败(polling 模式/URL 不可达)→ 去掉 callback 重试
            if callback_url and "callback url" in str(e).lower():
                log.warning("回调URL验证失败,去掉回调重试", 模式=mode, 错误=str(e)[:160])
                payload.pop("callback_url", None)
                data = await self._client.post("/video_generation", payload)
            else:
                raise
        task_id = str(data.get("task_id", ""))
        log.info("视频生成任务已受理", 模式=mode, 任务ID=task_id)
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
