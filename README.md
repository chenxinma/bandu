# 伴读 bandu

面向中小学生的 **语音陪伴型学习助手** 原型。
当前形态：**语音轮次对话**（学生说一句 → 伴读答一句），FastAPI WebSocket 后端 + 浏览器 HTML 前端。

> **已下线**：realtime 端到端「自然对话」通道（服务端 VAD + 抢话打断）。
> 换成轮次制的理由：教学话术要逐字可控（硬规则不过 LLM）、ASR/LLM/TTS 三段各自可换可调、
> 且浏览器只负责采集与播放，算力全在服务端，天然端-云分离。
> realtime 时期的设计与实测数据保留在 `docs/伴读S1技术方案.md`，新架构见 `docs/伴读WS轮次架构.md`。

# 架构

```text
浏览器（HTML + AudioWorklet）                 FastAPI 服务（Python asyncio，单进程）
┌────────────────────────────┐             ┌──────────────────────────────────────┐
│ 麦克风 → 重采样 → 16k PCM16  │──WS 二进制──▶│ EnergyVAD 收句（底噪自适应 + 800ms 尾静音）│
│ 字幕 / 状态灯 / 电平条        │             │   ↓ 学生说完                           │
│ 24k PCM16 排队播放 ←─────────│◀──WS 二进制──│ ASR  qwen-audio-3.0-asr-flash（3 份对冲）│
│ 「说完了」「跳过回答」按钮     │──WS JSON───▶│   ↓                                   │
│                            │◀──WS JSON───│ 规则硬兜底 / 教学 Agent qwen3.6-flash    │
└────────────────────────────┘             │   ↓ 按句切分                           │
                                           │ TTS  qwen-audio-3.0-tts-plus（流式）     │
                                           └──────────────────────────────────────┘
```

轮次状态机：`listening → thinking → speaking → listening`

+ thinking / speaking 期间上行音频一律丢弃（服务端丢帧 + 前端停止上行），**不支持抢话打断**
+ 「跳过回答」= `cancel`：掐掉本轮，伴读立刻回到 listening
+ 「说完了」= `speech_end`：手动断句，不必等 800ms 尾静音
+ 命中硬规则（直接要答案 / 跑题）不过 LLM，预置引导语逐字播报

# 运行

```bash
export TOKEN_PLAN_API_KEY=...   # 或 ANTHROPIC_AUTH_TOKEN；不展示、不落盘、不进日志
uv sync --all-groups            # 装依赖
uv run bandu                    # 打开 http://127.0.0.1:8000
```

浏览器里点「开始对话」→ 授权麦克风 → 说完一句停一下，伴读接话。`Ctrl+C` 结束服务。

```bash
uv run bandu --port 9000            # 换端口
uv run bandu --host 0.0.0.0         # 平板/局域网体验
uv run bandu --reload               # 开发模式，改代码自动重启
uvicorn bandu.server:app            # 等价，需要自定义 uvicorn 参数时直接用
```

> 麦克风权限只在**安全上下文**放开：`http://localhost` / `127.0.0.1` 可以直接用；
> 换成局域网 IP 或公网域名时必须走 https（自签证书或反向代理），否则前端拿不到麦克风。

# WS 协议

端点 `ws://<host>/ws`。**二进制帧 = 音频，文本帧 = JSON 事件。**

| 方向 | 内容 | 说明 |
|------|------|------|
| ↑ 二进制 | 16kHz / mono / PCM16，100ms 一帧（3200B） | 只在 `listening` 相位有意义，其余相位服务端丢弃 |
| ↑ JSON | `{"type":"speech_end"}` | 手动断句，立刻收尾开轮 |
| ↑ JSON | `{"type":"cancel"}` | 跳过本轮回答 |
| ↓ JSON | `ready` / `state` / `speech_start` / `user_text` / `assistant_delta` / `unheard` / `turn_end` / `error` / `pong` | 事件字段见 `docs/伴读WS轮次架构.md` |
| ↓ 二进制 | 24kHz / mono / PCM16 | TTS 语音，前端首尾相接排队播放 |

健康检查：`GET /healthz` → `{"ok": true, "api_key_configured": true}`（不回 Key 本身）。

# 对话日志与延迟

+ 每条连接一个会话 id，日志落 `.bandu-logs/<session_id>.jsonl`：转写、意图、规则命中、首字音频延迟
+ 延迟口径统一以**学生说完**为零点，`turn_end.timings` 回传 `asr_ms / first_sentence_ms / first_audio_ms`
+ 服务端控制台同步打字幕与延迟摘要（`ConsoleTee`），压测时不用盯浏览器

# 项目目录

```text
bandu/
├── src/bandu/
│   ├── server.py            # FastAPI 应用：/ 首页、/static、/ws、/healthz + 控制台旁路
│   ├── session.py           # 轮次状态机（核心）：VAD 收句 → ASR → 规则/Agent → TTS
│   ├── vad.py               # EnergyVAD：底噪自适应阈值、尾静音收尾、手动 flush
│   ├── asr.py               # 整段 ASR + 请求对冲（服务端排队抖动大）
│   ├── agent.py             # 教学 Agent：instructions + 流式按句切分
│   ├── tts.py               # 流式 TTS（DashScope WS 任务协议）
│   ├── rules.py             # 确定性意图分类 + 预置引导语（不调 LLM）
│   ├── session_log.py       # JSONL 对话日志 + 首字延迟埋点
│   ├── provider.py          # Token Plan 端点/模型名/音频契约/Key 读取
│   ├── state.py             # SessionState + TurnPhase
│   ├── prompts.py           # 人设与规则文案
│   └── static/              # index.html + app.js + capture.worklet.js
├── tests/                   # 48 个用例：VAD / 轮次状态机 / WS 协议 / 管道零件
├── scripts/demo.py          # 一键启动（等价 uv run bandu）
└── docs/
```

# 测试

```bash
uv run pytest -q                    # 不碰网络：ASR/LLM/TTS 全打桩
uv run ruff format src tests && uv run ruff check src tests
```
