# local-agent-senses

为纯文本 LLM Agent 提供本地视觉、视频和语音能力。默认使用本机 Ollama，
除非你主动配置 OpenAI 兼容端点，否则媒体不会上传到云端。

## 功能

- 图片理解、OCR 和逐字转录。
- 视频场景采样、联系表、时间窗口分析和字幕提取。
- 可选 FunASR 语音转文字，支持时间戳。
- Windows 屏幕与剪贴板捕获。
- CLI 和 MCP 两种调用方式。
- SSRF 防护、下载大小/时长限制，MCP 磁盘缓存默认关闭。

## 快速开始

需要 Python 3.10+、Ollama 和 ffmpeg。语音识别与视频网站解析是可选功能。

```bash
git clone https://github.com/Scheme0/local-agent-senses.git
cd local-agent-senses
ollama pull haervwe/GLM-4.6V-Flash-9B
ollama pull qwen3.5:4b
python vision.py --check
python vision.py photo.png --prompt "详细描述这张图片"
python vision.py document.png --mode text --transcribe
python vision.py clip.mp4 --mode scenes --prompt "按时间列出每个场景"
```

安装命令行入口：

```bash
pip install .
vision --check
vision-mcp
```

可选依赖：`pip install .[speech]`、`pip install .[ytdlp]`、
`pip install .[test]`。

## MCP

```bash
python extras/mcp_server.py
```

MCP 工具包括 `describe_image`、`transcribe`、`analyze_video`、
`transcribe_audio`、`vision_status` 和 `vision_check`。

服务结果使用统一 JSON 结构，包含 `text`、`kind`、`mode`、`metadata` 和
`warnings`。预期错误使用 `{ "code": ..., "message": ... }`，便于 MCP 客户端
区分参数错误、后端错误和媒体错误。

MCP 会校验图片数量、提示词长度、帧数和 FPS。结果只使用进程内缓存；磁盘
缓存默认关闭，因为结果可能包含截图、转录和私有文档。如确有需要再开启：

```bash
set VISION_MCP_CACHE=1       # Windows
export VISION_MCP_CACHE=1    # Linux/macOS
```

## 安全与隐私

HTTP/HTTPS 媒体 URL 会被当作不可信输入。回环地址、私有地址、链路本地地址、
组播地址、云元数据地址和 NAT64 地址会被阻止，重定向也会重复检查。

默认情况下，远程媒体先由 Python 安全下载，再交给 ffmpeg，避免 ffmpeg 重新
解析域名造成安全检查失效。可信环境可以显式开启直连：

```bash
set VISION_DIRECT_URL_STREAM=1       # Windows
export VISION_DIRECT_URL_STREAM=1    # Linux/macOS
```

详细配置和限制请查看英文版 README、`vision-config.example.json`、
`SECURITY.md` 和 `CHANGELOG.md`。

## 已知限制

- 视频理解基于抽帧，不是端到端时间推理。
- OCR 准确率取决于分辨率、字体、版式和模型。
- FunASR 需要额外环境，并可能首次下载模型。
- HEIC、AVIF 等格式取决于 ffmpeg 构建是否包含对应解码库。

## 开发

```bash
python -m pytest tests/ -v
python -m build
```

许可证为 MIT。
