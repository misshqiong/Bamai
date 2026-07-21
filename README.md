# Bamai（把脉）

Bamai（把脉）是面向 macOS Apple Silicon 的本地系统监控助手。*Take your Mac's pulse, locally.* / 给你的 Mac 把把脉。它实时采集 CPU、内存、磁盘、网络和进程状态，并可通过本机 Ollama + `qwen3:4b` 回答系统状态问题、查询历史数据和诊断异常。所有监控数据和 AI 请求都留在本机。

## 功能

- 3 秒实时 CPU、内存、磁盘 I/O 和网络曲线
- 1 小时至 7 天历史趋势和自动降采样
- CPU、内存、网络 Top 进程与异常事件
- 绿/黄/红三色健康横幅，以及按需触发的 AI 小白解读
- 中文 / English 即时切换，语言选择保存在浏览器本地
- Spotlight 文件名/内容搜索和限时大文件扫描
- 基于真实工具数据的本地 AI 问答
- CPU、内存、磁盘和网络异常的后台 AI 诊断
- Ollama 不可用时自动降级，监控和搜索功能继续工作
- 运行时切换本地模型、语言、温度和上下文长度，无需重启
- 插件化本地工具箱：网络、DNS、端口、Wi-Fi、电池、内存与受控抓包诊断

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
./bamai start
```

CLI 会创建 `.venv`、安装依赖、首次下载本地 ECharts，然后在后台启动：

```text
http://127.0.0.1:8737
```

服务只监听 `127.0.0.1`。SQLite 数据库默认位于 `~/.bamai/data.db`；可用 `BAMAI_DATA_DIR` / `BAMAI_DB_PATH` 覆盖数据位置，用 `BAMAI_OLLAMA_URL` 覆盖 Ollama 地址。首次启动时若只有 `~/.macpilot`，会把内容复制到 `~/.bamai` 并保留旧目录作为备份。

常用 CLI 命令：

```bash
./bamai status
./bamai logs
./bamai restart
./bamai stop
./bamai start --foreground
./bamai start --with-ai
./bamai model list
./bamai model use qwen3:8b
./bamai model pull qwen3:8b
./bamai autostart on
```

`run.sh` 保留为兼容入口，等同于 `./bamai start --foreground`。PID、服务日志和运行时设置分别位于 `~/.bamai/bamai.pid`、`~/.bamai/bamai.log` 和 `~/.bamai/config.json`。

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
- `GET /api/health`：不调用 AI 的当前健康规则结果
- `POST /api/health/explain`：按需生成当前健康的小白 AI 解读
- `GET /api/search/files`、`GET /api/search/large-files`：文件搜索
- `GET /api/ollama/status`：Ollama 与模型状态
- `GET/POST /api/settings`：读取或即时更新运行时设置
- `GET /api/ollama/models`：已安装与推荐模型
- `POST /api/ollama/pull`、`GET /api/ollama/pull/status`：后台下载模型与查询进度
- `GET /api/toolbox`、`POST /api/toolbox/{id}/run`：列出并运行诊断工具
- `GET /api/toolbox/jobs/{job_id}`、`POST /api/toolbox/{id}/explain`：轮询结果与 AI 解读

## 工具箱与抓包安全边界

所有探测命令都以参数列表直接启动，不经过 shell。抓包默认只保留 96 字节包头、不做域名反解，最长 60 秒、最多 2000 包；pcap 只写入 `~/.bamai/captures/`，不会进入 SQLite 或上传到网络。即使只保存包头，pcap 仍可能包含本机地址、端口和少量协议元数据，请仅在需要时运行并自行管理文件。

首次使用抓包时需按页面引导执行：

```bash
sudo ./scripts/enable-capture.sh
```

该脚本采用 Wireshark 同类的 `access_bpf` 用户组方案，不会让 Bamai 以 root 运行。完整撤销授权：

```bash
sudo ./scripts/disable-capture.sh
```
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

部分 macOS 环境可能限制 `nettop`。Bamai 会记录 warning 并跳过本次每进程网络采样，其他监控项不会中断。

### 数据与隐私

监控数据只写入本机 SQLite。AI 请求只发送到默认的本机地址 `http://localhost:11434`，不使用云端模型或 embedding 服务。
