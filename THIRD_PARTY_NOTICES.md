# Third-Party Notices

本项目以 MIT 许可分发，但运行时会使用/引用以下第三方组件。各自许可以其
上游仓库或模型页展示为准；本文件只是便利性汇总，不构成法律建议。

| 组件 | 用途 | 上游（许可以实际为准） |
|---|---|---|
| Ollama | 本地模型推理运行时 | https://github.com/ollama/ollama |
| GLM-4.6V-Flash-9B（`haervwe/GLM-4.6V-Flash-9B`） | 逐字转录 / OCR 模型 | https://ollama.com/haervwe/GLM-4.6V-Flash-9B |
| Qwen3.5-4B（`qwen3.5:4b`） | 快速看图 / 视频模型 | https://ollama.com/library/qwen3.5:4b |
| FunASR / SenseVoice | 语音转写 | https://github.com/modelscope/FunASR |
| yt-dlp | 通用站点直链解析（可选） | https://github.com/yt-dlp/yt-dlp |
| ffmpeg | 视频抽帧 / 音频解码 / 格式转码 | https://ffmpeg.org/legal.html |

说明：

- 模型权重不在本仓库内分发；`ollama pull` 下载时以模型页展示的许可为准。
- 语音模型（FunASR）首次使用从 ModelScope 下载到本地缓存，许可以其仓库为准。
- 仓库不内置任何二进制（Ollama / ffmpeg / 模型权重均需用户自行安装或拉取）。
