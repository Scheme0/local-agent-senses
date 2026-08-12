# 工具适配与实测状态

本仓库的定位是"一个 CLI，处处可用"：核心 `vision.py` 与工具无关，任何能执行
shell 命令的 agent 都能调用；针对各家工具的"自动触发"机制，在安装时按环境
自动生成对应的规则文件（`scripts/generate_adapters.py`，由 setup 脚本最后执行）。
下表是当前适配与实测状态，随验证持续更新。

| 通道 | 机制 | 状态 | 说明 |
|---|---|---|---|
| CLI（直接调用） | `python vision.py ...` | ✅ 已实测 | 2026-08-06 本机（Windows + RTX 4070 Ti SUPER）健康检查 5/5、中文逐字转录正确 |
| Ollama 原生后端 | 默认 | ✅ 已实测 | 全功能（图片/视频/语音/自检） |
| OpenAI 兼容端点 | `api_base` / `api_key` | ✅ 已实测 | 指向本机 Ollama `/v1` 完成图片转录与状态查询 |
| MCP stdio | `extras/mcp_server.py` | ✅ 协议层已实测 | initialize / tools/list / tools/call / ping / 错误分支 / 结果缓存（内存+磁盘）单测通过 |
| 通用站点解析（yt-dlp） | `YtDlpResolver`（可选依赖） | ✅ 已实测 | 所有站点链接统一经 yt-dlp 解析（按机制分类，不按站点分块）；B 站链接实测成功，YouTube 因本机网络不可达未实测 |
| Codex skill | `SKILL.md` | ✅ 已实测 | 本项目开发过程全程使用 |
| Claude Code | 安装时生成 `CLAUDE.md` | ⏳ 机制就绪，待实机验证 | 见下方验证步骤 |
| Cursor | 安装时生成 `.cursor/rules/vision.mdc` | ⏳ 机制就绪，待实机验证 | 见下方验证步骤 |
| Cline / Roo Code | 安装时生成 `.clinerules/vision.md` | ⏳ 机制就绪，待实机验证 | 见下方验证步骤 |
| Gemini CLI | 安装时生成 `GEMINI.md` | ⏳ 机制就绪，待实机验证 | 见下方验证步骤 |
| OpenHands / Continue 等 | 安装时生成 `AGENTS.md` | ⏳ 机制就绪，待实机验证 | 见下方验证步骤 |
| 一键 MCP 注册 | 生成 `.mcp.json`（`--agents mcp`） | ✅ 已实测 | 生成文件结构与内容单测通过；客户端读取注册待实机验证 |
| 截屏 / 剪贴板 | `python vision.py --capture screen\|clipboard` | ✅ / ⏳ | Windows 截屏 2026-08-08 实测生成 PNG 成功；剪贴板桥接与截屏共用同一 PowerShell 实现，机制就绪待实机验证 |

## 各工具验证步骤

### Claude Code

```bash
python /path/to/local-agent-senses/scripts/generate_adapters.py --agents claude --out /path/to/your/project
```

然后发一张本地图片路径，观察 Claude Code 是否调用 `python vision.py`。

### Cursor

```bash
python /path/to/local-agent-senses/scripts/generate_adapters.py --agents cursor --out /path/to/your/project
```

然后问："帮我看看
`C:\path\screenshot.png` 里有什么文字"，观察 Agent 模式是否自动调用
`vision.py`。

### Cline / Roo Code

```bash
python /path/to/local-agent-senses/scripts/generate_adapters.py --agents cline --out /path/to/your/project
```

然后发图片路径，观察 Cline / Roo Code 是否自动调用 `vision.py`。

### Gemini CLI

```bash
python /path/to/local-agent-senses/scripts/generate_adapters.py --agents gemini --out /path/to/your/project
```

然后在项目目录提问图片内容，观察 Gemini CLI 是否自动调用 `vision.py`。

### OpenHands / Continue / 其它读 AGENTS.md 的工具

```bash
python /path/to/local-agent-senses/scripts/generate_adapters.py --agents codex --out /path/to/your/project
```

然后提问，观察是否自动调用 `vision.py`。

### 任何支持 MCP 的客户端

```bash
python /path/to/local-agent-senses/extras/mcp_server.py
```

按客户端文档注册 stdio MCP server 后，直接使用 `describe_image` /
`transcribe` / `analyze_video` / `transcribe_audio` 工具。

也可以直接生成标准 `.mcp.json`（Claude Code / Cursor / Windsurf 等客户端会
自动读取项目根目录的该文件）：

```bash
python /path/to/local-agent-senses/scripts/generate_adapters.py --agents mcp --out /path/to/your/project
```

## 已知边界

- **GUI 粘贴的图片**：聊天里直接粘贴的附件仍需要用户先保存成文件再给路径
  （所有工具行为一致）；本机屏幕/剪贴板可用
  `python vision.py --capture screen|clipboard` 直接抓取。
- **媒体 URL 防 SSRF**：只允许公网 http/https，内网/回环/云元数据地址被拦截；
  ffmpeg 直读的流在初始 URL 校验后由其自行处理后续跳转。
- **远程/沙箱 agent**：如果 agent 跑在云 VM 或隔离沙箱里，访问不到本机
  Ollama；此时需配置 `api_base` 指向可达的 OpenAI 兼容端点。
- **自带视觉的模型**：当前模型原生支持图片时不需要本技能。
- **看门狗**：默认使用跨平台 `vram_watchdog.py`（Windows 旧版
  `vram-watchdog.ps1` 仍兼容），且只在本地 Ollama 模式下有意义；
  远程端点模式自动跳过。
- **语音转写**：需要 conda 环境与 FunASR；没有时其余功能不受影响。
- **宿主 MCP 超时**：本地视觉推理比纯文本慢（首次加载模型 5~15 秒、转录可能
  数十秒），部分宿主默认 MCP 超时只有 30~60 秒，首次调用可能被掐断；建议把
  该 MCP 服务的超时调到 120 秒以上（Codex 对应 `startup_timeout_sec`，其它
  客户端在 MCP 配置里找 timeout/timeoutMs 字段）。

## 本机实测记录（2026-08-06 / 2026-08-08）

- 环境：Windows，Python 3.14，RTX 4070 Ti SUPER 16GB，Ollama 本地模型
  `haervwe/GLM-4.6V-Flash-9B` + `qwen3.5:4b`
- `python -m pytest tests/ -v`：通过（纯函数 + 抽帧方案 + 格式嗅探 + 图片
  管线 + 后端消息构造 + MCP 协议 + 站点解析器 + 适配生成 + SSRF 防护 +
  截屏命令构造 + 打包元数据）
- `python vision.py --check`：5/5 通过（看图 / 转录 / stdin / 视频生成 / 抽帧）
- 端到端：含中英文测试图逐字转录完全正确；OpenAI 兼容路径（Ollama `/v1`）
  转录同样正确；B 站站点链接经统一 yt-dlp 通道解析成功
- 2026-08-08：Windows 截屏实测成功（`--capture screen` 生成 PNG）；
  pip 安装验证通过（`vision` / `vision-mcp` / `vision-adapters` 三个命令可用）
