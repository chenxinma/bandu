# 伴读 bandu · WS 语音轮次架构

> 本文取代 `docs/伴读S1技术方案.md` 中的 realtime（方案 A）章节与 `pipeline.py`（方案 B）落地方式。
> 现状：**只有一条通道** —— FastAPI WebSocket 服务 + 浏览器前端，语音轮次对话。

---

## 1. 为什么从「自然对话」退回「轮次对话」

realtime 端到端模型（`qwen-audio-3.0-realtime-plus` + `Agent.realtime()`）体验最顺：服务端 VAD、
抢话打断 0.14s 停播。但它把 ASR/LLM/TTS 焊死在一个模型里，伴读最看重的两件事做不了：

| 诉求 | realtime 端到端 | 轮次管道（现方案） |
|------|-----------------|--------------------|
| 「直接要答案」100% 不给答案 | 靠 prompt + `interrupt()` 补救，时序不可控 | 命中规则**不过 LLM**，预置话术逐字播报 |
| 换 ASR / LLM / TTS、单独调教学话术 | ❌ 锁死 | ✅ 三段各自可换 |
| 端-云分离（学生用平板，算力在服务端） | 需要设备侧跑 realtime 会话 | ✅ 浏览器只采集/播放 |
| 抢话打断 | ✅ | ❌ 用「跳过回答」按钮替代 |
| 说完 → 开口 | 0.88–1.48s | 1.7–2.5s（ASR 整段上传是大头） |

结论：**牺牲抢话打断，换教学可控性与部署形态**。轮次制对小学生场景并不别扭——
伴读说话时学生本来就该听，想插话点一下「跳过回答」。

---

## 2. 总体架构

```text
浏览器（一个 HTML 页面 + AudioWorklet）        FastAPI 服务（Python 3.13 / asyncio 单进程）
┌───────────────────────────────┐         ┌────────────────────────────────────────────┐
│ getUserMedia（回声消除/降噪/AGC）│         │ /            index.html                     │
│   ↓ AudioWorklet               │         │ /static      app.js / capture.worklet.js    │
│ 线性插值重采样 → 16k Int16      │         │ /healthz     存活 + Key 是否配好             │
│   ↓ 每 100ms 一帧              │         │ /ws          一条连接 = 一场会话             │
│ ws.send(ArrayBuffer) ──────────┼────────▶│   VoiceSession（session.py）                │
│                               │         │   ├ EnergyVAD   底噪自适应 + 800ms 尾静音     │
│ 状态灯 / 字幕 / 电平条          │         │   ├ ASR         qwen-audio-3.0-asr-flash    │
│                               │         │   │             （3 份对冲取最先回来的）      │
│ AudioBuffer 队列（24k）◀───────┼─────────│   ├ rules.py    硬兜底：要答案 / 跑题         │
│ 首尾相接排播，跳过时清空         │         │   ├ Agent       qwen3.6-flash + 关思考       │
│                               │◀────────│   │             run_stream 按句切分          │
│ 「开始/结束」「说完了」「跳过回答」│         │   ├ TTS         qwen-audio-3.0-tts-plus     │
└───────────────────────────────┘         │   └ SessionLog  .bandu-logs/<sid>.jsonl     │
                                          └────────────────────────────────────────────┘
```

**音频契约**（`provider.py` 是唯一事实源）

| 项 | 上行（麦克风） | 下行（TTS） |
|----|----------------|-------------|
| 采样率 | 16 000 Hz | 24 000 Hz |
| 声道 / 位深 | mono / int16 | mono / int16 |
| 帧长 | 1600 样本 = 100ms = 3200 B | TTS 服务返回多少转多少 |

前端不假设设备采样率：`AudioContext` 用默认采样率（多为 48k），worklet 内做线性插值降到 16k，
小数读指针跨 `process()` 连续，不会在帧缝上丢样本或产生周期性咔哒。

---

## 3. 轮次状态机

```text
        ┌──────────────── listening ────────────────┐
        │  上行音频喂 VAD；其余相位的帧一律丢弃         │
        └───┬───────────────────────────────────────┘
   VAD 尾静音 800ms / 20s 上限 / 前端 speech_end
            ▼
        thinking ──── ASR（3 份对冲）→ rules.classify
            │                              │
            │            命中 ASK_ANSWER / OFF_TOPIC → 预置话术（不过 LLM）
            │                              │
            ▼                              ▼
        speaking ──── Agent.run_stream 按句切 → TTS 流式合成 → 二进制下行
            │
        turn_end（timings）→ 回到 listening
```

关键实现点（都在 `session.py`）：

1. **相位同步切换**：VAD 收句后 `_start_turn()` 先把 `phase = THINKING` 再 `create_task`。
   上行帧可能在轮次任务被调度前就连着到达，晚一步关门就会又攒出一句、开出第二轮。
2. **建任务后 `await asyncio.sleep(0)`**：让轮次任务先跑进自己的 `try`。否则紧随其后的
   `cancel` 可能落在它第一个 `await` 之前，协程体一行没执行，`finally` 里的收尾
   （发 `turn_end`、相位回 `listening`）不会发生，会话就永久卡在 thinking 变僵尸。
3. **兜底恢复**：`_on_turn_done` 回调发现相位还停在 thinking/speaking（说明收尾没跑），
   补一次 `_end_turn(cancelled=True)`。只在「回调对应的仍是当前轮次」时才做，避免上一轮的
   迟到回调把新一轮的 `turn_end` 抢先发出去。
4. **收尾即复位**：`_end_turn()` 里把 `_echo_guard` 置为 2 帧 —— 播完后前 200ms 的上行帧丢弃，
   免得把喇叭余音当成学生开口（前端 `echoCancellation` + 播放期静音是第二道防线）。
5. **异常不掀连接**：ASR/LLM/TTS 任一段炸了，只发 `error`（`fatal:false`）+ `turn_end`，
   相位回 `listening`，学生可以立刻重说。只有 Key 没配这类问题会在每轮重复报。
6. **`aclosing` 关流**：跳过或断开时用 `contextlib.aclosing` 显式关掉 TTS 的 WS 与句子流，
   不把合成任务留在后台烧钱。

---

## 4. WS 协议

端点：`ws://<host>/ws`。**二进制帧 = 音频，文本帧 = JSON。** 事件都带 `type` 字段。

### 4.1 上行（浏览器 → 服务）

| 帧 | 内容 | 服务端行为 |
|----|------|-----------|
| 二进制 | 16k/mono/int16 PCM，100ms 一帧 | `listening` 相位喂 VAD；其他相位 `dropped_frames++` 后丢弃 |
| 文本 | `{"type":"speech_end"}` | 手动断句：`vad.flush()` 立刻收尾开轮；没攒到语音则回 `unheard{reason:"empty"}` |
| 文本 | `{"type":"cancel"}` | 取消进行中的轮次 → `turn_end{cancelled:true}` → 回 `listening` |
| 文本 | `{"type":"ping"}` | 回 `pong{ts}` |
| 文本 | 其他 / 坏 JSON | 回 `error{fatal:false}`，连接不断（坏 JSON 直接丢帧） |

### 4.2 下行（服务 → 浏览器）

| 事件 | 字段 | 何时发 |
|------|------|--------|
| `ready` | `session_id`, `log_path`, `audio{input_sample_rate, output_sample_rate, frame_ms}` | 连接建立后第一条，前端据此配采样率 |
| `state` | `state` ∈ `listening/thinking/speaking`, `turn` | 每次相位切换（前端据此决定采不采音、按钮亮不亮） |
| `speech_start` | `noise_floor`, `threshold` | VAD 判定学生开口，附当前底噪与阈值（调设备用） |
| `user_text` | `text`, `intent`, `rule_triggered` | ASR 落定，前端出「你」气泡 |
| `assistant_delta` | `text`, `rule_triggered` | 每切出一句就发一次，前端流式追加字幕 |
| 二进制 | 24k/mono/int16 PCM | TTS 语音，前端 `AudioBuffer` 首尾相接排播 |
| `unheard` | `seconds` 或 `reason:"empty"` | ASR 返回空 / 手动断句时没攒到语音 |
| `turn_end` | `turn_id`, `reply`, `cancelled`, `timings{asr_ms, first_sentence_ms, first_audio_ms}` | 本轮收尾（含空轮、被跳过、出错的轮） |
| `error` | `message`, `fatal` | 本轮作废；`fatal:true` 表示会话没法继续 |
| `pong` | `ts` | 回应 `ping` |

一轮正常问答的下行时序：

```text
ready → state(listening) → speech_start → state(thinking) → user_text
      → state(speaking) → assistant_delta × N（与二进制音频帧交错）
      → turn_end → state(listening)
```

### 4.3 前端侧的轮次约束

`app.js` 里上行有两道闸：

```js
if (!running || phase !== "listening" || playingLocally()) return;  // 不发
```

`playingLocally()` 是必要的：服务端音频发完就切回 `listening`，但浏览器排队播放往往还没散尽，
这时上行会把伴读自己的声音喂回 VAD。UI 上这段显示「播放中…」，`说完了` 按钮保持禁用。

---

## 5. 延迟口径与实测

所有延迟以**学生说完**（VAD 扣掉尾静音的时刻）为零点，与服务端 `turn_end.timings` 一致：

| 指标 | 含义 | 实测（合成语音，同口径） |
|------|------|--------------------------|
| `asr_ms` | 说完 → ASR 文本落定 | 1.1–1.3s（3 份对冲后净 ~0.7s） |
| `first_sentence_ms` | 说完 → LLM 第一句切出 | +0.6–0.7s |
| `first_audio_ms` | 说完 → 第一块 TTS 音频发出 | 1.7–2.5s |

> Token Plan **没有流式 ASR**，且整段接口的服务端排队抖动极大（同一句实测 0.29–10.5s，
> 与音频长短无关），所以 `asr.py` 默认发 3 份取最先回来的（`ASR_FANOUT=3`，每份都完整计费）。
> 服务端整体慢的相位里 3 份会一起慢，这是已知上限；改回 1 就是单发。
> LLM 必须显式 `enable_thinking:false`，否则 qwen3 系列首字从 0.4s 掉到 4.9s。

浏览器侧还有一段**播放排队延迟**（首块到 → 出声），本地回环约 20–60ms，不计入上表。

---

## 6. 模块与职责

| 文件 | 职责 | 备注 |
|------|------|------|
| `server.py` | FastAPI 装配：静态页、`/ws`、`/healthz`；`WebSocketDownstream`（发送失败即静默）；`ConsoleTee`（字幕/延迟打到服务端控制台） | `create_app(console=False)` 供测试静音 |
| `session.py` | **轮次状态机**：`VoiceSession`、`turn_timings`；`Downstream` 协议 | 与传输无关，可用假 downstream 单测 |
| `vad.py` | `EnergyVAD`（逐帧喂入）、`Utterance`、`record_utterance` | 阈值 = max(100, 底噪 20 分位 × 3)，尾静音 800ms，单句上限 20s |
| `asr.py` | `wav()` 封装、`transcribe()`、`transcribe_hedged()`、`extract_text()` | 全挂时抛异常，不伪装成「没听清」 |
| `agent.py` | `build_agent()`、`stream_sentences()`、`cut_sentences()` | `history` 就地累积上下文（服务端不记对话） |
| `tts.py` | `synthesize()`：DashScope WS 任务协议流式合成 | `START_TIMEOUT=15`，异常也要让下游收工 |
| `rules.py` | `classify()` / `canned_reply()`：确定性硬兜底，不调 LLM | 未改 |
| `session_log.py` | JSONL 日志 + 首字延迟埋点 | 未改 |
| `provider.py` | 端点、模型名、音频契约、`get_api_key()` / `auth_headers()` | Key 只读环境变量 |
| `state.py` | `SessionState` + `TurnPhase` | 移除了 realtime 时代的 `is_tts_playing` / `interrupted` |
| `static/` | `index.html` / `app.js` / `capture.worklet.js` | 无构建步骤，原生 ES module |

**已删除**：`runtime.py`（realtime 会话组装）、`voice.py`（sounddevice I/O）、`pipeline.py`
（本地 CLI 一问一答，逻辑已拆进上面几个模块）。依赖相应去掉 `pydantic-ai-slim[openai-realtime]`
的 realtime extra 与 `sounddevice`，加入 `fastapi` + `uvicorn[standard]`。

---

## 7. 运行与部署注意

```bash
export TOKEN_PLAN_API_KEY=...
uv run bandu                        # http://127.0.0.1:8000
uv run bandu --host 0.0.0.0         # 平板/局域网
```

+ **麦克风权限需要安全上下文**：`localhost` / `127.0.0.1` 直接可用；换成局域网 IP 或域名必须上 https。
+ 一条 WS 连接 = 一场会话 = 一个日志文件；连接之间互不共享上下文。
+ 单进程 asyncio 足够 demo；并发多路时注意 ASR 对冲会把请求数 ×3。
+ `/healthz` 只回 Key 配没配，绝不回 Key；日志与控制台也不落 Key。
+ Key 没配时 `bandu` 启动即退出并给提示；真跑起来后 ASR 会回 401，前端每轮收到 `error` 气泡但连接不断。
+ **`/ws` 没有鉴权**（demo 定位）：对外暴露前必须自己加一层（网关鉴权 / 会话票据），
  否则任何人都能拿你的 Key 额度跑 ASR/LLM/TTS。

---

## 8. 验收对照（原 S1 清单）

| 验收项 | 现状 |
|--------|------|
| 连续对话 10 轮不崩溃 | ✅ 轮次任务互斥 + 异常不掀连接；`tests/test_session.py` 覆盖多轮 |
| 用户说话后 2 秒内有语音回应 | ⚠️ 1.7–2.5s，卡在整段 ASR；对冲后多数轮次达标，服务端慢相位会超 |
| 打断后 1 秒内停止播放 | 🔁 改为「跳过回答」：本地立刻停播 + 服务端 `cancel` 掐轮次 |
| 明显非学习话题被引导回来 | ✅ `rules.OFF_TOPIC`，不过 LLM |
| 直接问答案不给完整答案 | ✅ `rules.ASK_ANSWER`，预置话术逐字播报 |
| 对话日志可查看 | ✅ `.bandu-logs/<session_id>.jsonl` |
| 弱网下不卡死，有降级提示 | ✅ 每段有超时；失败发 `error` 后回 `listening` |

---

## 9. 测试

```bash
uv run pytest -q        # 48 passed，全程不碰网络
```

| 文件 | 覆盖 |
|------|------|
| `tests/test_vad.py` | 底噪自适应、绝对下限、句中停顿不切断、超长截断、`flush`、`discard`、跨轮底噪保留 |
| `tests/test_session.py` | 事件时序、日志落盘、硬兜底不过 LLM、空转写、**非听相位丢帧**、手动断句、`cancel`、后端异常、多轮上下文 |
| `tests/test_server_ws.py` | 真 FastAPI + TestClient：首页/静态资源、`/healthz` 不泄 Key、WS 整轮时序与二进制音频、坏控制消息 |
| `tests/test_pipeline_units.py` | WAV 封装、ASR 响应解析各种形状、对冲全挂要抛异常、切句规则、帧契约 |
| `tests/test_rules.py` / `test_session_log.py` | 原有：意图分类与预置话术、日志字段与首字延迟每轮只记一次 |

打桩点在 `bandu.session` 命名空间（`transcribe_hedged` / `stream_sentences` / `synthesize`），
所以测试跑的是真实状态机，只把三段外部服务换成假的。
