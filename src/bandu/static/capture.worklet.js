// AudioWorklet：麦克风 Float32（多为 48kHz）→ 16kHz Int16，每 100ms 发一帧给主线程。
// 帧长与服务端 VAD 对齐（provider.FRAME_SAMPLES = 1600），线性插值重采样，跨 process 连续。
const TARGET_RATE = 16000;
const FRAME_SAMPLES = 1600;

class BanduCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buffer = new Int16Array(FRAME_SAMPLES);
    this._filled = 0;
    this._sum = 0;
    this._tail = new Float32Array(0); // 上一次没消费完的样本
    this._pos = 0; // 小数读指针，跨 process 调用连续
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;

    const merged = new Float32Array(this._tail.length + channel.length);
    merged.set(this._tail);
    merged.set(channel, this._tail.length);

    const step = sampleRate / TARGET_RATE; // sampleRate = 采集上下文采样率
    let pos = this._pos;
    while (pos + 1 < merged.length) {
      const index = Math.floor(pos);
      const frac = pos - index;
      this._push(merged[index] * (1 - frac) + merged[index + 1] * frac);
      pos += step;
    }
    const consumed = Math.floor(pos);
    this._tail = merged.slice(consumed);
    this._pos = pos - consumed;
    return true;
  }

  _push(sample) {
    const value = Math.max(-1, Math.min(1, sample));
    this._buffer[this._filled++] = value * 0x7fff;
    this._sum += value * value;
    if (this._filled === FRAME_SAMPLES) this._flush();
  }

  _flush() {
    const pcm = this._buffer.buffer; // 转移所有权，本地立刻换新缓冲
    // rms 用 int16 量级，和服务端 VAD 的阈值/底噪同一把尺子
    const rms = Math.sqrt((this._sum / FRAME_SAMPLES) * 0x7fff * 0x7fff);
    this.port.postMessage({ pcm, rms }, [pcm]);
    this._buffer = new Int16Array(FRAME_SAMPLES);
    this._filled = 0;
    this._sum = 0;
  }
}

registerProcessor("bandu-capture", BanduCapture);
