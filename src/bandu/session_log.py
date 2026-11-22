"""会话 JSONL 日志：转写、规则命中、首字音频延迟（不落 Key）。"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .rules import Intent

LOG_DIR = Path(".bandu-logs")


class SessionLog:
    """一次会话一个 JSONL 文件，一行一条记录。"""

    def __init__(self, session_id: str, directory: Path | str = LOG_DIR) -> None:
        self.session_id = session_id
        self.path = Path(directory) / f"{session_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.turn_id = 0
        self._turn_mark: float | None = None

    def user_turn(
        self, text: str, intent: Intent, *, rule_triggered: bool, since: float | None = None
    ) -> None:
        """记录学生这句话，并以此刻作为本轮首字延迟的起点。

        `since` 用于把零点回拨到“学生说完”（方案 B 的 VAD 已知该时刻，而转写晚于它）。
        """
        self.turn_id += 1
        self._turn_mark = time.monotonic() if since is None else since
        self._write(event="user", text=text, intent=intent.value, rule_triggered=rule_triggered)

    def assistant_turn(self, text: str) -> None:
        """记录助手这句话。"""
        self._write(event="assistant", text=text)

    def first_audio(self) -> None:
        """首个音频块到达时打点：每轮只记一次，本轮已记或未开新轮时是空操作。"""
        if self._turn_mark is None:
            return
        elapsed_ms = round((time.monotonic() - self._turn_mark) * 1000)
        self._turn_mark = None
        self._write(event="first_audio", first_audio_ms=elapsed_ms)

    def _write(self, **fields: Any) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            **fields,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
