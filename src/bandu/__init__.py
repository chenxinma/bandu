"""伴读 bandu：面向中小学生的语音陪伴型学习助手（S1 DEMO）。"""

import asyncio
import sys

from .pipeline import run as run_pipeline
from .runtime import run


def main() -> None:
    """启动 S1 语音对话：默认方案 A（realtime），`--pipeline` 走方案 B。"""
    session = run_pipeline if "--pipeline" in sys.argv else run
    try:
        asyncio.run(session())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # 弱网/服务异常不抛栈，只给屏幕提示（方案 §10 最后一档降级）。
        print(f"网络不佳或服务异常，请稍后再试。（{type(exc).__name__}: {exc}）")
