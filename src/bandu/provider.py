"""Token Plan 接入配置：端点、模型名、音频契约与 Key 读取。

三段（ASR / LLM / TTS）各自独立可换：不再走 realtime 端到端模型，
轮次由服务端 VAD + 状态机决定（见 `session.py`）。
"""

import os

BASE_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com"
ASR_URL = f"{BASE_URL}/api/v1/services/aigc/multimodal-generation/generation"
TTS_WS_URL = f"{BASE_URL}/api-ws/v1/inference/"
LLM_BASE_URL = f"{BASE_URL}/compatible-mode/v1"

ASR_MODEL = "qwen-audio-3.0-asr-flash"
TTS_MODEL = "qwen-audio-3.0-tts-plus"
# 实测首字 0.40s、输出干净；qwen3.8-flash 0.57s 且会反问学生已给的条件。
LLM_MODEL = "qwen3.6-flash"
# qwen3 系列默认开思考，首字会从 0.4s 掉到 4.9s，必须显式关掉。
LLM_SETTINGS = {"extra_body": {"enable_thinking": False}}
VOICE = "longanqian"

# 音频契约：浏览器上行 16kHz、TTS 下行 24kHz，都是单声道 PCM16。
INPUT_SAMPLE_RATE = 16_000
OUTPUT_SAMPLE_RATE = 24_000
FRAME_SAMPLES = 1_600  # 100ms/帧
FRAME_SECONDS = FRAME_SAMPLES / INPUT_SAMPLE_RATE


def get_api_key() -> str:
    """读取 Token Plan Key（优先 TOKEN_PLAN_API_KEY，其次 ANTHROPIC_AUTH_TOKEN）。"""
    key = os.environ.get("TOKEN_PLAN_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not key:
        raise RuntimeError(
            "Token Plan API Key 未配置：请设置 TOKEN_PLAN_API_KEY 或 ANTHROPIC_AUTH_TOKEN"
        )
    return key


def auth_headers() -> dict[str, str]:
    """ASR（HTTP）与 TTS（WS）共用的鉴权头。Key 只走请求头，不落日志。"""
    return {"Authorization": f"Bearer {get_api_key()}"}
