"""MiniMax 文件管理 —— Files API(上传 / 检索,mm_file:// 引用)。"""
from __future__ import annotations

import httpx

from app.logging import get_logger
from app.minimax.client import AllKeysFailedError, MiniMaxClient, MiniMaxError, mask_key

log = get_logger("minimax.files")


class FilesAPI:
    def __init__(self, client: MiniMaxClient) -> None:
        self._client = client

    async def upload(self, data: bytes, filename: str, purpose: str = "file-extract") -> str:
        """上传文件,返回 file_id(供 mm_file://{file_id} 引用)。

        multipart 上传不走 JSON 通道,这里手动实现同样的多 Key fallback:
        每 key 重试 1 次 → 切下一个 → 全败抛 AllKeysFailedError。
        """
        import asyncio

        attempts: list[dict] = []
        url = f"{self._client._base_url}/files/upload"
        for key_idx, key in enumerate(self._client._keys, start=1):
            for attempt in (1, 2):
                try:
                    log.info("文件上传开始", 文件名=filename, 用途=purpose,
                             大小KB=round(len(data) / 1024, 1),
                             Key序号=key_idx, 尝试=attempt)
                    resp = await self._client._http.post(
                        url,
                        headers={"Authorization": f"Bearer {key}"},
                        data={"purpose": purpose},
                        files={"file": (filename, data)},
                    )
                    if resp.status_code in (401, 403):
                        raise MiniMaxError(1004, f"HTTP {resp.status_code} 鉴权被拒")
                    if resp.status_code == 429:
                        raise MiniMaxError(1002, "HTTP 429 限流")
                    if resp.status_code >= 500:
                        raise MiniMaxError(1002, f"HTTP {resp.status_code} 服务端错误")
                    resp.raise_for_status()
                    body = resp.json()
                    MiniMaxClient._check_base_resp(body)
                    file_id = str((body.get("file") or {}).get("file_id", ""))
                    log.info("文件上传成功", 文件名=filename, 文件ID=file_id, Key序号=key_idx)
                    return file_id
                except MiniMaxError as e:
                    if not e.retryable:
                        log.error("文件上传请求级错误(不重试)", 文件名=filename,
                                  错误码=e.code, 详情=e.msg)
                        raise
                    attempts.append({"key_index": key_idx, "key": mask_key(key),
                                     "attempt": attempt, "error": f"code={e.code} {e.msg}"})
                    log.warning("文件上传失败", 文件名=filename, Key序号=key_idx,
                                尝试=attempt, 错误码=e.code, 详情=e.msg)
                except (httpx.TimeoutException, httpx.HTTPError) as e:
                    attempts.append({"key_index": key_idx, "key": mask_key(key),
                                     "attempt": attempt,
                                     "error": f"{type(e).__name__}: {e}"})
                    log.warning("文件上传网络异常", 文件名=filename, Key序号=key_idx,
                                尝试=attempt, 异常类型=type(e).__name__, 详情=str(e))
                if attempt == 1:
                    await asyncio.sleep(0.5)
        raise AllKeysFailedError("/files/upload", attempts)

    async def retrieve_url(self, file_id: str) -> str:
        """按 file_id 取临时下载 URL(24h 失效,需即时下载)。"""
        data = await self._client.get("/files/retrieve", params={"file_id": file_id})
        f = data.get("file") or {}
        url = str(f.get("download_url") or f.get("url") or "")
        log.info("文件检索完成", 文件ID=file_id, 已获取URL=bool(url))
        return url

    async def download(self, url: str) -> bytes:
        """下载临时 URL 内容(生成的视频/音频等)。"""
        resp = await self._client._http.get(url)
        resp.raise_for_status()
        data = resp.content
        log.info("文件下载完成", 大小KB=round(len(data) / 1024, 1))
        return data
