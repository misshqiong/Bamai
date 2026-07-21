# MacPilot

MacPilot 是面向 macOS Apple Silicon 的本地系统监控助手。它实时采集 CPU、内存、磁盘、网络和进程状态，并可通过本机 Ollama + `qwen3:4b` 回答系统状态问题、查询历史数据和诊断异常。所有监控数据和 AI 请求都留在本机。

## 功能

- 3 秒实时 CPU、内存、磁盘 I/O 和网络曲线
- 1 小时至 7 天历史趋势和自动降采样
- CPU、内存、网络 Top 进程与异常事件
- Spotlight 文件名/内容搜索和限时大文件扫描
- 基于真实工具数据的本地 AI 问答
- CPU、内存、磁盘和网络异常的后台 AI 诊断
- Ollama 不可用时自动降级，监控和搜索功能继续工作

## 环境要求

- macOS（Apple Silicon）
- Python 3.12+
- 可选：Ollama 和 `qwen3:4b` 模型

如需 AI 功能，先执行：

```bash
brew install ollama && ollama pull qwen3:4b
```

安装后确保 Ollama 服务正在运行。未安装 Ollama 时，聊天栏会显示同一安装命令和“重新检测”按钮。

## 一键启动

```bash
cd /Users/heqiong/Documents/code/ai_local
./run.sh
```

脚本会创建 `.venv`、安装依赖、首次下载本地 ECharts，然后启动：

```text
http://127.0.0.1:8737
```

服务只监听 `127.0.0.1`。SQLite 数据库默认位于 `~/.macpilot/data.db`；可用 `MACPILOT_DB_PATH` 覆盖数据库路径，用 `MACPILOT_OLLAMA_URL` 覆盖 Ollama 地址。

## 使用 AI 助手

聊天栏可以处理例如：

- “现在 CPU 和内存怎么样？”
- “过去 6 小时内存峰值是多少？”
- “哪个进程最占网络？”
- “今天有哪些异常事件？”
- “帮我找 Downloads 中超过 500MB 的文件。”

Agent 最多进行 6 轮工具调用，所有回答必须依据本机工具返回的数据。聊天响应下方可展开查看查询过程。删除文件、杀进程等危险操作只会给出建议，不会代替用户执行。

## API

- `GET /api/overview`：当前总览及 Ollama 状态
- `GET /api/metrics`：历史指标
- `GET /api/processes`、`GET /api/processes/net`：进程状态
- `GET /api/events`：异常事件及 AI 诊断
- `GET /api/search/files`、`GET /api/search/large-files`：文件搜索
- `GET /api/ollama/status`：Ollama 与模型状态
- `POST /api/chat`：本地 Agent 对话
- `WS /ws/realtime`：3 秒实时指标

## 测试

```bash
cd /Users/heqiong/Documents/code/ai_local
.venv/bin/python -m pytest -q
```

Agent、聊天 API 和事件诊断测试全部使用 mock Ollama 响应，不要求安装或运行真实 Ollama。

## 常见问题

### 聊天栏显示 Ollama 未就绪

执行：

```bash
brew install ollama && ollama pull qwen3:4b
```

确认 Ollama 已启动后点击聊天栏中的“重新检测”。

### nettop 采集失败

部分 macOS 环境可能限制 `nettop`。MacPilot 会记录 warning 并跳过本次每进程网络采样，其他监控项不会中断。

### 数据与隐私

监控数据只写入本机 SQLite。AI 请求只发送到默认的本机地址 `http://localhost:11434`，不使用云端模型或 embedding 服务。
