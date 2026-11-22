# 伴读 bandu

面向中小学生的 语音陪伴型学习助手 原型项目。
当前阶段目标是验证流畅语音对话闭环：孩子说话 → 流式识别 → 教学智能体处理 → 流式语音回复 → 支持打断。

# 当前能力

+ 流式语音对话（方案 A：realtime 端到端）
+ 方案 B 管道通道（VAD + 整段 ASR + 文本 Agent + 流式 TTS，`--pipeline`）
+ 百炼实时模型接入
+ 基础学习模式拦截
+ 对话日志记录
+ 打断播放

# 运行

```bash
export TOKEN_PLAN_API_KEY=...   # 或 ANTHROPIC_AUTH_TOKEN，不展示、不落盘
uv run bandu                     # 方案 A：realtime 端到端，支持抢话打断（等价：uv run python scripts/demo.py）
uv run bandu --pipeline           # 方案 B：VAD + 整段 ASR + 文本 Agent + 流式 TTS，一问一答
```

开口即对话，Ctrl+C 结束。方案 B 每轮会在屏幕上打延迟摘要（`ASR / 首句 / 开口`，均以“学生说完”为零点）。
两条通道都把转写、规则命中与首字音频延迟写入 `.bandu-logs/<session_id>.jsonl`（方案 B 文件名带 `-pipeline` 后缀）。

# 项目目录

```text
bandu/                     # 仓库根
├── src/bandu/             # 源码（S1 扫平铺模块，映射见 docs/伴读S1技术方案.md §11）
│   ├── runtime.py         # 方案 A 会话组装：麦克风/扬声器/转写循环 + 规则硬兜底
│   ├── pipeline.py        # 方案 B：VAD 录音 → ASR → 文本 Agent → 流式 TTS
│   ├── provider.py        # Token Plan realtime 模型适配（base_url / profile / turn_detection）
│   ├── agent.py           # 教学 Agent（instructions）
│   ├── rules.py           # 确定性意图分类 + 预置引导语
│   ├── session_log.py     # JSONL 对话日志 + 首字延迟埋点
│   ├── voice.py           # 麦克风/扬声器 I/O（16k 入 / 24k 出）
│   ├── state.py           # SessionState
│   └── prompts.py         # 人设与规则文案
├── tests/
├── scripts/
├── docs/
├── pyproject.toml
└── README.md
```