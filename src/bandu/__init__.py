"""伴读 bandu：面向中小学生的语音陪伴型学习助手。

架构：FastAPI WS 服务后端 + HTML 前端，**语音轮次对话**（说一句答一句，不支持抢话打断）。
realtime 端到端自然对话已下线，历史设计见 `docs/伴读S1技术方案.md`。
"""

import argparse


def main() -> None:
    """启动 WS 服务：浏览器打开首页，点「开始对话」即可一轮一轮说。"""
    parser = argparse.ArgumentParser(prog="bandu", description="伴读 bandu 语音轮次对话服务")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址；平板/局域网体验用 0.0.0.0（需 https 才有麦克风）",
    )
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    parser.add_argument("--reload", action="store_true", help="开发模式：改代码自动重启")
    args = parser.parse_args()

    import uvicorn

    from .provider import get_api_key

    try:
        get_api_key()
    except RuntimeError as exc:
        raise SystemExit(f"{exc}。Key 只读环境变量，不展示、不落盘、不进日志。") from exc

    shown = "localhost" if args.host in {"0.0.0.0", "::"} else args.host
    print(f"伴读 bandu：http://{shown}:{args.port}   （Ctrl+C 结束）", flush=True)
    # 用导入字符串而不是 app 对象，--reload 才生效
    uvicorn.run("bandu.server:app", host=args.host, port=args.port, reload=args.reload)
