"""伴读 bandu：面向中小学生的语音陪伴型学习助手（S1 DEMO）。"""

import asyncio

from .runtime import run


def main() -> None:
    """启动 S1 实时语音对话闭环。"""
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # 弱网/服务异常不抛栈，只给屏幕提示（方案 §10 最后一档降级）。
        print(f"网络不佳或服务异常，请稍后再试。（{type(exc).__name__}: {exc}）")
