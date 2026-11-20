"""会话状态（Pydantic 模型）。"""

from typing import Literal

from pydantic import BaseModel


class SessionState(BaseModel):
    session_id: str = "sess_001"
    user_id: str = "student_001"
    mode: Literal["learning", "chat"] = "learning"
    last_topic: str | None = None
    answer_unlock_level: int = 1
    help_coins: int = 3
    is_tts_playing: bool = False
    interrupted: bool = False
