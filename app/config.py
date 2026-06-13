"""应用配置 —— pydantic-settings 读取 .env。

MiniMax API Key 支持配置多个(逗号分隔),按顺序 fallback:
每个 key 失败后重试 1 次,再失败切换下一个 key,全部失败才向用户报错。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ── Telegram ───────────────────────────────────────────────
    bot_token: str = ""
    webhook_host: str = ""
    webhook_secret: str = "secret"
    mode: str = "webhook"  # webhook | polling
    superadmin_ids: str = ""

    # ── MiniMax ────────────────────────────────────────────────
    # 多 Key:逗号分隔;兼容旧的单 Key 变量 MINIMAX_API_KEY
    minimax_api_keys: str = ""
    minimax_api_key: str = ""  # 兼容旧配置(单个 key)
    minimax_base_url: str = "https://api.minimaxi.com/v1"
    mmx_callback_url: str = ""

    # ── 模型 ───────────────────────────────────────────────────
    model_chat: str = "MiniMax-M3"
    model_summary: str = "MiniMax-M2.7-highspeed"
    model_tts: str = "speech-2.8-hd"
    model_image: str = "image-01"
    model_video: str = "MiniMax-Hailuo-2.3"
    model_music: str = "music-2.6"

    # ── 搜索 / 抓取 ────────────────────────────────────────────
    firecrawl_api_key: str = ""
    brave_api_key: str = ""
    search_order: str = "firecrawl,brave,duckduckgo"
    search_result_count: int = 5
    search_retry: int = 1
    firecrawl_timeout_s: float = 15.0
    brave_timeout_s: float = 8.0
    ddg_timeout_s: float = 8.0

    # ── 时间 ───────────────────────────────────────────────────
    default_tz: str = "Asia/Shanghai"

    # ── 鉴权 / 配额 ────────────────────────────────────────────
    permission_denied_text: str = "⛔ Permission Denied"
    default_quota_mode: str = "tokens"  # tokens | calls
    default_quota_limit: int = 200_000  # -1 = 无限
    default_quota_period: str = "day"  # day | month | total
    gen_call_weights: str = "image:1,video:5,music:5,tts:1,search:1"

    # ── 并发 / 背压 ────────────────────────────────────────────
    max_concurrent_chats: int = 32
    max_concurrent_generations: int = 8
    per_user_concurrency: int = 3
    tg_global_send_rate: float = 28.0  # msg/s
    worker_poll_interval_s: float = 5.0
    httpx_max_connections: int = 100

    # ── 行为 / 存储 ────────────────────────────────────────────
    default_token_budget: int = 128_000
    compact_trigger_ratio: float = 0.6
    edit_throttle_ms: int = 1500
    db_path: str = "./data/bot.db"
    sqlite_wal: bool = True
    log_level: str = "INFO"

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("webhook", "polling"):
            raise ValueError(f"MODE 必须是 webhook 或 polling,当前值: {v!r}")
        return v

    @model_validator(mode="after")
    def _merge_legacy_key(self) -> "Settings":
        # 旧的单 Key 变量并入多 Key 列表(若多 Key 未配置)
        if not self.minimax_api_keys.strip() and self.minimax_api_key.strip():
            self.minimax_api_keys = self.minimax_api_key.strip()
        return self

    # ── 派生属性 ───────────────────────────────────────────────
    @property
    def minimax_keys(self) -> list[str]:
        """解析后的 MiniMax API Key 列表(已去空白、去重、保持顺序)。"""
        seen: set[str] = set()
        keys: list[str] = []
        for raw in self.minimax_api_keys.split(","):
            k = raw.strip()
            if k and k not in seen:
                seen.add(k)
                keys.append(k)
        return keys

    @property
    def superadmin_id_list(self) -> list[int]:
        out: list[int] = []
        for raw in self.superadmin_ids.split(","):
            raw = raw.strip()
            if raw:
                try:
                    out.append(int(raw))
                except ValueError:
                    continue
        return out

    @property
    def search_order_list(self) -> list[str]:
        return [p.strip().lower() for p in self.search_order.split(",") if p.strip()]

    @property
    def call_weights(self) -> dict[str, int]:
        """生成类调用的 calls 权重,如 {'image': 1, 'video': 5, ...}"""
        out: dict[str, int] = {}
        for pair in self.gen_call_weights.split(","):
            pair = pair.strip()
            if ":" in pair:
                name, _, val = pair.partition(":")
                try:
                    out[name.strip()] = int(val.strip())
                except ValueError:
                    continue
        return out

    @property
    def db_file(self) -> Path:
        return Path(self.db_path)


@lru_cache
def get_settings() -> Settings:
    return Settings()
