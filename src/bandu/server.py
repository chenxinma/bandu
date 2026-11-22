"""FastAPI 服务：HTML 前端 + `/ws` 语音轮次通道。

    uv run bandu                 # 起服务，浏览器打开 http://127.0.0.1:8000
    uvicorn bandu.server:app     # 等价，需要自定义参数时直接调 uvicorn

WS 协议见 `docs/伴读WS轮次架构.md`：上行二进制 = 16kHz PCM16 音频帧，
上行文本 = JSON 控制消息；下行文本 = JSON 事件，下行二进制 = 24kHz PCM16 语音。
"""

import json
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .provider import get_api_key
from .session import Inbound, VoiceSession
from .session_log import SessionLog
from .state import SessionState

STATIC_DIR = Path(__file__).parent / "static"


class WebSocketDownstream:
    """把会话事件写成 JSON 文本帧、语音写成二进制帧。连接断了就静默丢弃。"""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self.closed = False

    async def send_event(self, event: str, **fields: Any) -> None:
        await self._guard(lambda: self._ws.send_json({"type": event, **fields}))

    async def send_audio(self, chunk: bytes) -> None:
        await self._guard(lambda: self._ws.send_bytes(chunk))

    async def _guard(self, send: Callable[[], Coroutine[Any, Any, Any]]) -> None:
        if self.closed:
            return
        try:
            await send()
        except Exception:  # 客户端已断开：让轮次自己收尾，别把异常抛回会话
            self.closed = True


class ConsoleTee:
    """下行旁路：字幕与延迟摘要同时打到服务端控制台（前端才是主显示）。"""

    def __init__(self, inner: WebSocketDownstream) -> None:
        self._inner = inner
        self._reply_open = False

    async def send_event(self, event: str, **fields: Any) -> None:
        self._echo(event, fields)
        await self._inner.send_event(event, **fields)

    async def send_audio(self, chunk: bytes) -> None:
        await self._inner.send_audio(chunk)

    def _echo(self, event: str, fields: dict[str, Any]) -> None:
        if event == "user_text":
            rule = "   [规则硬兜底]" if fields.get("rule_triggered") else ""
            print(f"你: {fields.get('text', '')}{rule}", flush=True)
        elif event == "assistant_delta":
            if not self._reply_open:
                print("伴读: ", end="", flush=True)
                self._reply_open = True
            print(fields.get("text", ""), end="", flush=True)
        elif event == "turn_end":
            if self._reply_open:
                self._reply_open = False
                print(flush=True)
            if fields.get("cancelled"):
                print("  [本轮已跳过]", flush=True)
            elif timings := fields.get("timings"):
                parts = " · ".join(f"{key} {value}ms" for key, value in timings.items())
                print(f"  [{parts}]", flush=True)
        elif event == "unheard":
            print("（没听清，再说一次？）", flush=True)
        elif event == "error":
            print(f"! {fields.get('message')}", flush=True)


async def inbound_messages(ws: WebSocket) -> AsyncIterator[Inbound]:
    """读上行：二进制 → 音频帧，文本 → JSON 控制消息，断开 → 结束。"""
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return
        if (audio := message.get("bytes")) is not None:
            yield audio
            continue
        if (text := message.get("text")) is None:
            continue
        try:
            yield json.loads(text)
        except json.JSONDecodeError:
            continue  # 坏帧不值得断连，丢掉即可


def create_app(*, console: bool = True) -> FastAPI:
    """装配 FastAPI 应用（测试可传 `console=False` 静音）。"""
    app = FastAPI(title="伴读 bandu", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, Any]:
        """存活检查：只报 Key 配没配，绝不回 Key 本身。"""
        try:
            get_api_key()
            configured = True
        except RuntimeError:
            configured = False
        return {"ok": True, "api_key_configured": configured}

    @app.websocket("/ws")
    async def voice_socket(ws: WebSocket) -> None:
        await ws.accept()
        state = SessionState()
        log = SessionLog(state.session_id)
        downstream: WebSocketDownstream = WebSocketDownstream(ws)
        session = VoiceSession(
            ConsoleTee(downstream) if console else downstream, state=state, log=log
        )
        print(f"[会话开始] {state.session_id}  日志：{log.path}", flush=True)
        try:
            await session.run(inbound_messages(ws))
        except WebSocketDisconnect:
            pass
        finally:
            await session.shutdown()
            print(
                f"[会话结束] {state.session_id}  轮次 {state.turns}"
                f"  非听相位丢帧 {session.dropped_frames}",
                flush=True,
            )

    return app


app = create_app()
