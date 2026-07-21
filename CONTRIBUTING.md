# Contributing / 贡献指南

Thank you for helping improve Bamai. Keep changes focused, local-first, and safe for non-technical Mac users.

感谢你帮助改进 Bamai。请保持改动聚焦、本地优先，并确保普通 Mac 用户可以安全使用。

## Tests / 测试

Create a virtual environment, install development tools, then run both required checks:

创建虚拟环境并安装开发工具，然后运行两项必需检查：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt pytest ruff
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
```

Tests must not require a real Ollama service, network access, root privileges, or packet-capture permission. Use mocks and fixed output samples.

测试不得依赖真实 Ollama、网络访问、root 权限或抓包授权；请使用 mock 和固定输出样例。

## Adding a probe / 新增探针

1. Add one module under `server/toolbox/probes/` with a parser, a runner, and `get_spec()`.
2. Describe parameters with `ProbeParam`; validate ranges and character sets before running anything.
3. Run commands only with `subprocess.run([...])`. Never use `shell=True` or concatenate user input into a command string.
4. Return a bounded `ProbeResult` with structured `summary`, optional `rows`, raw output, and artifacts.
5. Register the probe in `server/toolbox/registry.py` and add all UI text to both i18n dictionaries.
6. Add parser tests using fixed sample output, validation/injection tests, and API or Agent tests when applicable.
7. Add a probe to the Agent allowlist only when it needs no authorization and completes within 35 seconds.

中文步骤：

1. 在 `server/toolbox/probes/` 下新增单独模块，包含解析器、runner 和 `get_spec()`。
2. 用 `ProbeParam` 描述参数，在执行命令前严格校验范围和字符集。
3. 只能使用 `subprocess.run([...])`，禁止 `shell=True`，也不要把用户输入拼进命令字符串。
4. 返回有界的 `ProbeResult`，包含结构化 `summary`、可选 `rows`、原始输出和 artifacts。
5. 在 `server/toolbox/registry.py` 注册，并把 UI 文案同时加入两种语言的 i18n 字典。
6. 使用固定输出样例补充解析测试、参数注入测试，以及适用的 API/Agent 测试。
7. 只有免授权且能在 35 秒内完成的探针才可加入 Agent 白名单。

## Code style / 代码风格

- Python targets 3.12+ and must pass Ruff with `E`, `F`, `I`, and `W` enabled.
- Keep lines within 100 characters and prefer small, typed functions.
- Preserve API contracts, local-only behavior, and graceful degradation when optional macOS tools or Ollama are unavailable.
- Do not commit personal paths, credentials, generated databases, packet captures, caches, or vendored downloads.
- Keep English and Chinese i18n key sets identical.

中文说明：Python 面向 3.12+，必须通过 Ruff；行宽不超过 100 字符，优先使用小而有类型标注的函数。不得破坏 API、本地运行边界或 Ollama 不可用时的降级行为；不得提交个人路径、凭证、数据库、抓包、缓存或下载生成物；中英文字典键集合必须一致。
