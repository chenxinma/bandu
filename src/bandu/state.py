"""会话状态（Pydantic 模型）与轮次相位。"""

import uuid
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class TurnPhase(StrEnum):
    """轮次相位：同一时刻只在一个相位上，思考/播报期间不收学生音频（不支持抢话）。"""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


def new_session_id() -> str:
    """一条 WS 连接一个会话 id（同时用作日志文件名）。"""
    return f"sess_{uuid.uuid4().hex[:8]}"


class SessionState(BaseModel):
    """一次语音会话的状态。"""

    session_id: str = Field(default_factory=new_session_id)
    user_id: str = "student_001"
    mode: Literal["learning", "chat"] = "learning"
    last_topic: str | None = None
    answer_unlock_level: int = 1
    help_coins: int = 3
    phase: TurnPhase = TurnPhase.IDLE
    turns: int = 0  # 已完成的轮次数（含“没听清”的空轮）
