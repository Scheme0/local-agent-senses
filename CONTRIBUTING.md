# 参与贡献

欢迎提交 Issue 与 PR。项目很小，约定也简单。

## 开发环境

- Python 3.10+（本机开发使用 3.14）
- 跑单元测试不需要 GPU / Ollama：`python -m pytest tests/ -v`
- 完整自检需要本机 Ollama 与两个默认模型：`python vision.py --check`

## 提交前检查

1. `python -m pytest tests/ -v` 全部通过；
2. 有条件时运行 `python vision.py --check`，确认 5 条链路就绪；
3. 不提交个人路径与敏感内容：`vision-config.json`、API Key、本机截图、
   模型目录等（大部分已在 `.gitignore`）；
4. 文档要诚实：未经实机验证的通道标注 ⏳，不要写成 ✅；
5. 修改 MCP 工具时，同步更新 `tests/test_mcp.py` 和双语 README 中的工具表。

## 代码约定

- 根目录的 7 个 Python 文件（`config.py`、`media.py`、`media_formats.py`、
  `ollama_client.py`、`service.py`、`video_plans.py`、`vision.py`）只依赖标准库
  （视觉路径零第三方依赖）；
- `extras/` 是可选模块，删掉不应影响核心功能；
- 新增环境变量统一在 `config.py` 定义默认值，并在 SKILL.md / README 配置表中说明；
- README.md 为英文主文档，中文说明维护在独立的 README.zh-CN.md；修改内容时同步更新两种语言。

## 提交信息

建议使用 Conventional Commits 风格：`feat:` / `fix:` / `docs:` / `test:` / `refactor:`。
