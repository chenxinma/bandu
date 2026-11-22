"""FastAPI 服务：首页/静态资源、健康检查、WS 协议时序与二进制音频帧。"""

import json

from fastapi.testclient import TestClient
from support import spoken_clip

from bandu import server
from bandu.session_log import SessionLog


def receive_message(ws) -> bytes | dict:
    """收一条下行：JSON 事件 → dict，语音 → bytes。"""
    message = ws.receive()
    assert message["type"] == "websocket.send", message
    if message.get("bytes") is not None:
        return message["bytes"]
    return json.loads(message["text"])


def receive_event(ws) -> dict:
    """只取下一个 JSON 事件，途中的音频帧丢掉。"""
    while isinstance(item := receive_message(ws), bytes):
        continue
    return item


def test_index_page_is_served():
    with TestClient(server.create_app(console=False)) as client:
        index = client.get("/")
    assert index.status_code == 200
    assert "开始对话" in index.text
    assert "/static/app.js" in index.text


def test_static_assets_are_served():
    with TestClient(server.create_app(console=False)) as client:
        app_js = client.get("/static/app.js")
        worklet = client.get("/static/capture.worklet.js")
    assert app_js.status_code == 200
    assert "bandu-capture" in app_js.text  # 前端得引用同名 worklet processor
    assert worklet.status_code == 200
    assert "registerProcessor" in worklet.text


def test_healthz_does_not_leak_key():
    with TestClient(server.create_app(console=False)) as client:
        body = client.get("/healthz").json()
    assert body["ok"] is True
    assert set(body) == {"ok", "api_key_configured"}


def test_ws_round_trip(monkeypatch, tmp_path, stubs):
    """连上 → 喂一句音频 → 收到字幕/语音/延迟摘要 → 回到 listening。"""
    monkeypatch.setattr(server, "SessionLog", lambda sid: SessionLog(sid, directory=tmp_path))

    with (
        TestClient(server.create_app(console=False)) as client,
        client.websocket_connect("/ws") as ws,
    ):
        ready = receive_event(ws)
        assert ready["type"] == "ready"
        assert ready["audio"] == {
            "input_sample_rate": 16_000,
            "output_sample_rate": 24_000,
            "frame_ms": 100,
        }
        assert receive_event(ws) == {"type": "state", "state": "listening", "turn": 0}

        for chunk in spoken_clip():
            ws.send_bytes(chunk)

        events: list[dict] = []
        audio = b""
        while True:
            item = receive_message(ws)
            if isinstance(item, bytes):
                audio += item
                continue
            events.append(item)
            if item["type"] == "turn_end":
                break

        assert [event["type"] for event in events] == [
            "speech_start",
            "state",
            "user_text",
            "state",
            "assistant_delta",
            "assistant_delta",
            "turn_end",
        ]
        assert events[1]["state"] == "thinking"
        assert events[2]["text"] == stubs.text
        assert events[3]["state"] == "speaking"
        assert audio == b"".join(stubs.audio * len(stubs.sentences))
        assert events[-1]["reply"] == "".join(stubs.sentences)
        assert events[-1]["timings"]["first_audio_ms"] >= 0
        assert receive_event(ws) == {"type": "state", "state": "listening", "turn": 1}


def test_ws_control_message_over_the_wire(tmp_path, stubs, monkeypatch):
    """控制消息走 JSON 文本帧：坏消息只回 error，不断连。"""
    monkeypatch.setattr(server, "SessionLog", lambda sid: SessionLog(sid, directory=tmp_path))

    with (
        TestClient(server.create_app(console=False)) as client,
        client.websocket_connect("/ws") as ws,
    ):
        receive_event(ws)  # ready
        receive_event(ws)  # state: listening
        ws.send_json({"type": "unknown"})
        error = receive_event(ws)
        assert error["type"] == "error"
        assert error["fatal"] is False
        ws.send_json({"type": "ping"})  # 连接还活着，后续事件照常
        assert receive_event(ws)["type"] == "pong"
