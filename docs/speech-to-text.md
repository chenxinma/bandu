调用 Token Plan 语音识别 API，将音频转写为文字。

音频文件或 URL：$ARGUMENTS

## 步骤

1. 确认输入是本地音频路径或公网可访问的 URL。音频时长不超过 5 分钟，格式和大小限制以百炼语音识别文档中的音频规格为准。
2. 安装依赖：pip install requests
3. 使用 Bash 工具执行以下 Python 脚本完成识别。脚本优先读取 TOKEN_PLAN_API_KEY，其次读取 ANTHROPIC_AUTH_TOKEN；不要展示 Key，也不要写入文件或日志。

```python
import base64
import json
import os
import sys
from pathlib import Path

import requests

MIME_TYPES = {
    "aac": "audio/aac",
    "amr": "audio/amr",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "opus": "audio/opus",
    "wav": "audio/wav",
    "webm": "audio/webm",
}

source = sys.argv[1]
key = os.environ.get("TOKEN_PLAN_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
if not key:
    raise SystemExit("Token Plan API Key not found.")

if source.startswith(("http://", "https://")):
    audio_data = source
    audio_format = Path(source.split("?")[0]).suffix.lstrip(".").lower() or "mp3"
else:
    path = Path(source)
    audio_format = path.suffix.lstrip(".").lower()
    mime_type = MIME_TYPES.get(audio_format, f"audio/{audio_format}")
    audio_data = f"data:{mime_type};base64,{base64.b64encode(path.read_bytes()).decode()}"

payload = {
    "model": "qwen-audio-3.0-asr-flash",
    "input": {
        "messages": [
            {
                "role": "user",
                "content": [{"type": "input_audio", "input_audio": {"data": audio_data}}],
            }
        ]
    },
    "parameters": {"format": audio_format},
}

response = requests.post(
    "https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    json=payload,
    timeout=300,
)
if not response.ok:
    raise SystemExit(f"{response.status_code}: {response.text}")
result = response.json()
print(result.get("output", {}).get("text") or json.dumps(result, ensure_ascii=False, indent=2))
```

4. 输出识别文本。需要详细结果时，展示返回 JSON 中的 `output.sentence.text` 和 `output.sentence.words` 字段。