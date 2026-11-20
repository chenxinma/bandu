# 伴读 bandu

面向中小学生的 语音陪伴型学习助手 原型项目。
当前阶段目标是验证流畅语音对话闭环：孩子说话 → 流式识别 → 教学智能体处理 → 流式语音回复 → 支持打断。

# 当前能力

+ 流式语音对话
+ 百炼实时模型接入
+ 基础学习模式拦截
+ 对话日志记录
+ 打断播放

# 运行

```bash
export TOKEN_PLAN_API_KEY=...   # 或 ANTHROPIC_AUTH_TOKEN，不展示、不落盘
uv run bandu                     # 等价：uv run python scripts/demo.py
```

开口即对话，Ctrl+C 结束。每轮转写、规则命中与首字音频延迟写入 `.bandu-logs/<session_id>.jsonl`。

# 项目目录

```text
bandu/                     # 仓库根
├── src/bandu/             # 源码（S1 扫平铺模块，映射见 docs/伴读S1技术方案.md §11）
│   ├── runtime.py         # 会话组装：麦克风/扬声器/转写循环 + 规则硬兜底
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