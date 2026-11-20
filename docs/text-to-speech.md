调用 Token Plan 语音合成 API，将文本转为音频文件并下载到本地。

待合成文本：$ARGUMENTS

## 步骤

1. 提取 text（待合成文本）、voice（默认 longanhuan_v3.6）、format（默认 mp3）、sample_rate（默认 22050）。如果指定了模型，使用指定的模型名；否则默认 qwen-audio-3.0-tts-plus。
2. 使用 Bash 工具执行以下命令合成语音并下载：

```bash
RESPONSE=$(curl -s -X POST "https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer" \
  -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen-audio-3.0-tts-plus",
    "input": {
      "text": "<text>",
      "voice": "<voice>",
      "format": "<format>",
      "sample_rate": <sample_rate>
    }
  }')

AUDIO_URL=$(printf '%s' "$RESPONSE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["output"]["audio"]["url"])')
curl -s -o "speech_$(date +%Y%m%d_%H%M%S).<format>" "$AUDIO_URL"
```

3. 输出下载后的音频文件路径。