// 伴读 bandu 前端：麦克风 16kHz PCM16 上行，24kHz PCM16 下行排队播放。
// 轮次制的关键在前端这一半：只在「聆听中」且本地播放已散尽时才上行音频，
// 伴读思考/说话期间前端静音（服务端同样会丢弃非 listening 相位的帧）。

const PHASE_TEXT = {
  idle: "未连接",
  listening: "聆听中…",
  thinking: "思考中…",
  speaking: "回答中…",
  playing: "播放中…",
};
const TIMING_LABEL = { asr_ms: "ASR", first_sentence_ms: "首句", first_audio_ms: "开口" };

const dom = {
  phase: document.getElementById("phase"),
  session: document.getElementById("session"),
  transcript: document.getElementById("transcript"),
  level: document.getElementById("level"),
  vad: document.getElementById("vad"),
  toggle: document.getElementById("toggle"),
  done: document.getElementById("done"),
  skip: document.getElementById("skip"),
  hint: document.getElementById("hint"),
};

let ws = null;
let capture = null; // { ctx, stream, node }
let player = null; // { ctx, nextTime, sources }
let uploadHoldUntil = 0; // 播完再憋一小段采集：等房间余音/喇叭回声衰减，别让 VAD 收到自家回答的尾巴
let running = false;
let phase = "idle";
let rendered = ""; // 上一次画到屏幕上的相位，避免每帧重写 DOM
let botBubble = null; // 本轮伴读字幕气泡（流式追加）
let outputRate = 24000;

// ---------- 相位与按钮 ----------

function playingLocally() {
  return player !== null && player.sources.size > 0;
}

function renderPhase() {
  // 服务端说 listening，但喇叭里还排着没播完的音频 → 对学生来说仍是「伴读在说」
  const shown = playingLocally() ? "playing" : phase;
  if (shown === rendered) return;
  rendered = shown;
  dom.phase.dataset.phase = shown === "playing" ? "speaking" : shown;
  dom.phase.textContent = PHASE_TEXT[shown] ?? shown;
  dom.done.disabled = !(running && shown === "listening");
  dom.skip.disabled = !(running && shown !== "listening" && shown !== "idle");
}

function setHint(text) {
  dom.hint.textContent = text;
}

function setLevel(rms) {
  // 阈值量级参考服务端 VAD（底噪×3，下限 100），满刻度取 1200 够看
  dom.level.style.width = `${Math.min(100, Math.round((rms / 1200) * 100))}%`;
}

// ---------- 字幕 ----------

function bubble(kind, text = "") {
  const node = document.createElement("div");
  node.className = `bubble ${kind}`.trim();
  node.textContent = text;
  dom.transcript.append(node);
  dom.transcript.scrollTop = dom.transcript.scrollHeight;
  return node;
}

function meta(node, text) {
  const line = document.createElement("div");
  line.className = "meta";
  line.textContent = text;
  node.append(line);
  dom.transcript.scrollTop = dom.transcript.scrollHeight;
}

// ---------- 播放 ----------

function ensurePlayer() {
  if (player === null) player = { ctx: new AudioContext(), nextTime: 0, sources: new Set() };
  if (player.ctx.state === "suspended") player.ctx.resume();
  return player;
}

function closePlayer() {
  if (player === null) return;
  stopPlayback();
  player.ctx.close();
  player = null;
}

function playChunk(buffer) {
  const pcm = new Int16Array(buffer, 0, Math.floor(buffer.byteLength / 2));
  if (pcm.length === 0) return;
  const context = ensurePlayer().ctx;
  const audio = context.createBuffer(1, pcm.length, outputRate);
  const channel = audio.getChannelData(0);
  for (let index = 0; index < pcm.length; index += 1) channel[index] = pcm[index] / 32768;

  const source = context.createBufferSource();
  source.buffer = audio;
  source.connect(context.destination);
  // 首块立刻播，后续块首尾相接排队；20ms 余量避免接缝处咔哒
  const startAt = Math.max(context.currentTime, player.nextTime) + 0.02;
  source.start(startAt);
  player.nextTime = startAt + audio.duration;
  player.sources.add(source);
  source.onended = () => {
    player.sources.delete(source);
    if (player.sources.size === 0) uploadHoldUntil = performance.now() + 300;
    renderPhase();
  };
  renderPhase();
}

function stopPlayback() {
  if (player === null) return;
  for (const source of player.sources) {
    source.onended = null;
    try {
      source.stop();
    } catch {
      // 已经播完了，忽略
    }
  }
  player.sources.clear();
  player.nextTime = 0;
  uploadHoldUntil = performance.now() + 300;
  renderPhase();
}

// ---------- 采集 ----------

async function openMic() {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });
  const AudioContextClass = window.AudioContext ?? window.webkitAudioContext;
  const ctx = new AudioContextClass(); // 不指定采样率：重采样交给 worklet，兼容性最好
  if (!ctx.audioWorklet) throw new Error("当前浏览器不支持 AudioWorklet，请用新版 Chrome/Edge/Safari");
  await ctx.audioWorklet.addModule("/static/capture.worklet.js");
  const source = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, "bandu-capture");
  node.port.onmessage = (event) => onFrame(event.data);
  const mute = ctx.createGain();
  mute.gain.value = 0; // 只借渲染图把 worklet 拉起来，绝不把麦克风回放进喇叭
  source.connect(node);
  node.connect(mute).connect(ctx.destination);
  capture = { ctx, stream, node };
}

function onFrame({ pcm, rms }) {
  setLevel(rms);
  if (!running || phase !== "listening" || playingLocally() || performance.now() < uploadHoldUntil) return; // 轮次制：不上行
  if (ws !== null && ws.readyState === WebSocket.OPEN) ws.send(pcm);
}

// ---------- 协议 ----------

function send(payload) {
  if (ws !== null && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(payload));
}

function onEvent(message) {
  switch (message.type) {
    case "ready":
      outputRate = message.audio?.output_sample_rate ?? 24000;
      dom.session.textContent = `会话 ${message.session_id}`;
      setHint(`对话日志：${message.log_path}`);
      break;
    case "state":
      phase = message.state;
      if (phase === "thinking") botBubble = null; // 新一轮开始，字幕另起气泡
      renderPhase();
      break;
    case "speech_start":
      dom.vad.textContent = `底噪 ${message.noise_floor} · 阈值 ${message.threshold}`;
      break;
    case "user_text": {
      const node = bubble("user", message.text);
      if (message.rule_triggered) meta(node, "规则硬兜底");
      break;
    }
    case "assistant_delta":
      if (botBubble === null) botBubble = bubble("bot");
      botBubble.textContent += message.text;
      dom.transcript.scrollTop = dom.transcript.scrollHeight;
      break;
    case "unheard":
      bubble("system", "没听清，再说一次？");
      botBubble = null;
      break;
    case "turn_end": {
      const parts = Object.entries(message.timings ?? {}).map(
        ([key, value]) => `${TIMING_LABEL[key] ?? key} ${value}ms`,
      );
      if (message.cancelled) parts.push("已跳过");
      if (botBubble !== null) meta(botBubble, parts.join(" · "));
      else if (message.reply || parts.length > 0) meta(bubble("bot", message.reply), parts.join(" · "));
      botBubble = null;
      break;
    }
    case "error":
      bubble("error", message.message);
      break;
    default:
      break;
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  ws.onmessage = (event) => {
    if (typeof event.data !== "string") {
      playChunk(event.data); // 二进制帧 = 24kHz PCM16 语音
      return;
    }
    try {
      onEvent(JSON.parse(event.data));
    } catch {
      // 坏帧丢掉，不断连
    }
  };
  ws.onclose = () => {
    if (running) shutdown("连接已断开，点「开始对话」重来。");
  };
  ws.onerror = () => setHint("连接出错，检查后端是否还在跑。");
}

async function start() {
  dom.toggle.disabled = true;
  try {
    await openMic(); // 必须在用户手势里申请麦克风
    ensurePlayer(); // 播放上下文也在手势里建，否则部分浏览器不给 resume
    connect();
    running = true;
    dom.toggle.textContent = "结束对话";
    setHint("已连接：等状态变成「聆听中」再开口，说完停一下让伴读接话。");
    tick();
  } catch (error) {
    setHint(`启动失败：${error?.message ?? error}`);
    await shutdown();
  } finally {
    dom.toggle.disabled = false;
  }
}

async function shutdown(reason = "已结束。") {
  running = false;
  closePlayer();
  if (capture !== null) {
    capture.node.port.onmessage = null;
    capture.stream.getTracks().forEach((track) => track.stop());
    await capture.ctx.close();
    capture = null;
  }
  if (ws !== null) {
    ws.onclose = null;
    ws.close();
    ws = null;
  }
  phase = "idle";
  rendered = "";
  renderPhase();
  dom.toggle.textContent = "开始对话";
  setHint(reason);
}

function tick() {
  if (!running) return;
  renderPhase(); // 播放散尽的瞬间要把「播放中」翻回「聆听中」
  requestAnimationFrame(tick);
}

dom.toggle.addEventListener("click", () => (running ? shutdown() : start()));
dom.done.addEventListener("click", () => send({ type: "speech_end" }));
dom.skip.addEventListener("click", () => {
  stopPlayback(); // 本地立刻静音，再请服务端掐掉这一轮
  send({ type: "cancel" });
});
renderPhase();
