"""数据模型 —— 轻量 dataclass(配合手写 DAO,不引入重 ORM)。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class User:
    tg_id: int
    username: str | None = None
    first_name: str | None = None
    role: str = "user"  # superadmin | admin | user
    authorized: int = 0
    authorized_by: int | None = None
    authorized_at: int | None = None
    settings: str = "{}"
    created_at: int | None = None
    updated_at: int | None = None

    @property
    def is_admin(self) -> bool:
        return self.role in ("admin", "superadmin")

    @property
    def is_superadmin(self) -> bool:
        return self.role == "superadmin"

    @property
    def is_allowed(self) -> bool:
        """授权制:已授权或管理员即可用全部功能。"""
        return self.authorized == 1 or self.is_admin


@dataclass(slots=True)
class Quota:
    user_id: int
    mode: str  # calls | tokens
    period: str  # day | month | total
    limit_val: int  # -1 = 无限
    used: int = 0
    window_start: int | None = None
    updated_at: int | None = None

    @property
    def unlimited(self) -> bool:
        return self.limit_val < 0

    @property
    def remaining(self) -> int:
        if self.unlimited:
            return -1
        return max(0, self.limit_val - self.used)


@dataclass(slots=True)
class ChatRow:
    chat_id: int
    type: str = "private"
    title: str | None = None
    settings: str = "{}"
    token_budget: int = 128_000
    created_at: int | None = None


@dataclass(slots=True)
class MessageRow:
    id: int | None
    chat_id: int
    user_id: int | None
    role: str  # system | user | assistant | tool
    content: str
    content_type: str = "text"
    tokens: int = 0
    compacted: int = 0
    created_at: int | None = None


@dataclass(slots=True)
class Memory:
    id: int | None
    scope: str  # user | chat
    owner_id: int
    text: str
    source: str = "manual"  # manual | tool | auto
    weight: float = 1.0
    created_at: int | None = None
    last_used_at: int | None = None


@dataclass(slots=True)
class Generation:
    id: int | None
    user_id: int
    chat_id: int
    kind: str  # image | video | tts | music
    model: str
    prompt: str
    status: str = "queued"  # queued | processing | success | failed
    task_id: str | None = None
    file_id: str | None = None
    result_url: str | None = None
    placeholder_msg_id: int | None = None
    error: str | None = None
    created_at: int | None = None
    finished_at: int | None = None
