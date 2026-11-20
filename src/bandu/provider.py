"""Token Plan 实时语音模型适配。

qwen-audio-3.0-realtime-plus 走 OpenAI Realtime 协议，首选 OpenAIRealtimeModel 直连。
若 W1 验证 base_url → WS 路径映射不兼容，再改自定义 RealtimeModel 兜底。
"""

import os

from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.realtime.openai import OpenAIRealtimeModel, OpenAIRealtimeModelSettings

MODEL_NAME = "qwen-audio-3.0-realtime-plus"
REALTIME_BASE_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com/api-ws/v1"
VOICE = "longanqian"


def get_api_key() -> str:
    """读取 Token Plan Key（优先 TOKEN_PLAN_API_KEY，其次 ANTHROPIC_AUTH_TOKEN）。"""
    key = os.environ.get("TOKEN_PLAN_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not key:
        raise RuntimeError(
            "Token Plan API Key 未配置：请设置 TOKEN_PLAN_API_KEY 或 ANTHROPIC_AUTH_TOKEN"
        )
    return key


def build_realtime_model() -> OpenAIRealtimeModel:
    """构建指向 Token Plan 的实时语音模型。"""
    return OpenAIRealtimeModel(
        MODEL_NAME,
        provider=OpenAIProvider(base_url=REALTIME_BASE_URL, api_key=get_api_key()),
        settings=OpenAIRealtimeModelSettings(
            # 合成语音三档压测：400ms+high 把一句话切成 5 段并抢答半截问题，800ms 首字 2.2s，
            # 600ms+medium 一句一段、首字 1.2–1.5s、打断 0.14s。
            turn_detection={"sensitivity": "medium", "silence_duration_ms": 600},
            # shortcut: smart_turn_v2（Qwen 语义轮次）Pydantic AI 下发不了，需子类覆盖
            # _audio_input_config；实测它比 server_vad 慢（转写 1.6s vs 0.7s），真实儿童语音出现
            # “嗯/啊”误切分时再升级。
            reconnect={"max_attempts": 3, "max_reconnects": 5},  # 弱网掉线自动重连，历史轮次重放
            openai_voice=VOICE,
        ),
        # 模型名不被 Pydantic AI 识别，需显式给采样率与能力标志。
        profile={
            "audio_input_sample_rate": 16_000,
            "audio_output_sample_rate": 24_000,
            "supports_text_output": True,
            "supports_audio_output": True,
            "supports_interruption": True,
            "emits_input_speech_events": True,
            "supports_output_truncation": True,
        },
    )
