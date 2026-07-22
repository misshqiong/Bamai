# Bamai（把脉）

**一款本地优先的 Mac 健康监控与诊断助手，帮助你看懂 Mac 正在做什么。**

[English README](README.md)

## 隐私优先

Bamai 默认完全在本机运行：

- Web 服务只监听 `127.0.0.1`，不会暴露到局域网。
- 指标、提醒、设置和抓包文件只保存在 Mac 的 `~/.bamai/` 下。
- AI 请求只发往本机 Ollama。Bamai 没有云端后端、遥测或账号系统。
- 未安装 Ollama 时，监控与诊断功能仍然可用。

抓包文件仍可能包含本机地址、端口和协议元数据。Bamai 会限制抓包时间和包大小，但本地文件仍需由你自行妥善管理。

## 功能

- 实时 CPU、每核 CPU、内存、磁盘 I/O、网络和进程监控。
- SQLite 历史数据，以及一小时至七天的趋势图。
- 无需 AI 的规则提醒与绿/黄/红三色健康横幅。
- 可选的本地 AI 对话和面向普通用户的解释。
- 无需重启即可切换模型、语言、温度和上下文长度。
- 中文/英文界面即时切换。
- Spotlight 文件搜索和有界的大文件扫描。
- 插件式诊断工具箱，内置九个探针。
- 有界、仅保留包头的抓包，以及协议和 Top 会话汇总。
- 双语 `bamai` CLI，可管理守护进程、日志、模型和开机启动。

## 环境要求

- Apple Silicon Mac。
- Python 3.12 或更高版本。
- 可选：[Ollama](https://ollama.com/)，用于本地 AI 功能。

## 快速开始

```bash
git clone <repository-url> bamai
cd bamai
./bamai start
```

打开 <http://127.0.0.1:8737>。首次启动会创建 `.venv`、安装依赖并下载本地图表资源。使用 `./bamai status`、`./bamai logs` 和 `./bamai stop` 管理服务。

AI 是可选功能。启用方式：

```bash
brew install ollama
ollama serve
ollama pull qwen3:4b
./bamai restart --with-ai
```

旧的 `./run.sh` 入口仍然保留，用于前台启动 Bamai。

## 推荐模型

| 模型 | 约需下载 | 建议系统内存 | 适用场景 |
| --- | ---: | ---: | --- |
| `qwen3:4b` | 3 GB | 8 GB | 响应快，也是默认选择 |
| `qwen3:8b` | 6 GB | 16 GB | 更详细的解释 |
| `qwen3:14b` | 10 GB | 24 GB | 适合配置较高的 Mac，推理质量更好 |
| `llama3.1:8b` | 5 GB | 16 GB | 偏英文使用、喜欢 Llama 生态的用户 |

中文场景首选 Qwen3 系列；`llama3.1:8b` 是实测合格的英文向备选。

每个推荐模型都通过了两步工具调用实测（直连 Ollama 的单工具探测 + Bamai 完整 agent 链路测试）。一些知名模型虽然声明支持 `tools`，但实测从不发出结构化工具调用（`glm4:9b`、`mistral:7b`）或直接拒绝（`gemma3`）——它们无法通过 Bamai 读取你的系统数据，因此刻意不列入。

可以在“设置”页面安装或切换模型，也可以运行 `./bamai model pull <name>` 或 `./bamai model use <name>`。

## 工具箱

工具箱提供只读、结构化的本机诊断：

- Ping、路由追踪、DNS、端口连通性和 macOS `networkQuality`。
- 内存压力和 RSS 持续增长的进程。
- Wi-Fi 信号/信道信息与电池健康。
- 有界抓包，以及协议分布和五元组 Top 会话。

每个探针都会严格校验参数，并以参数列表直接调用系统命令，不经过 shell。后台任务最多同时运行三个。

### 抓包授权

当前用户能够读取 `/dev/bpf0` 之前，抓包功能保持禁用。执行：

```bash
sudo ./scripts/enable-capture.sh
```

脚本采用与 Wireshark 相同的 `access_bpf` 用户组方案，不会让 Bamai 服务以 root 身份运行。授权后需要退出登录并重新登录。若要移除 Bamai 创建的 LaunchDaemon、用户/组变更和 BPF 权限，执行：

```bash
sudo ./scripts/disable-capture.sh
```

抓包使用 96 字节 snaplen、不进行名称反解，最长 60 秒或最多 2,000 个包，只写入 `~/.bamai/captures/`。抓包不会写入 SQLite，也不会上传云端。

## 架构

```text
浏览器 UI（HTML/CSS/JavaScript）
          │ 127.0.0.1:8737 上的 REST + WebSocket
          ▼
FastAPI 服务 ───── SQLite（~/.bamai/data.db）
     │    │
     │    ├── 监控采集 + 规则引擎 + 后台任务
     │    └── 工具箱注册表 ── macOS 内置命令
     │
     └── 可选 Ollama 客户端 ── 127.0.0.1:11434 上的本地模型
```

采集器记录系统指标，规则引擎生成结构化事件，FastAPI 提供 REST/WebSocket 接口，静态仪表盘展示本地数据。Ollama 被隔离在可选客户端之后，因此 AI 离线不会影响监控。

## 截图

首个公开版本的占位链接：

- [仪表盘](docs/screenshots/dashboard.png)
- [工具箱](docs/screenshots/toolbox.png)
- [设置与本地 AI](docs/screenshots/settings-chat.png)

截图贡献说明见 [docs/screenshots/](docs/screenshots/)。

## 开发与测试

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt pytest ruff
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
```

以前台模式运行开发服务：

```bash
./bamai start --foreground
```

测试不依赖 Ollama，AI 响应全部使用 mock。新增探针和代码风格说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 数据与配置

- 数据库：`~/.bamai/data.db`
- 运行时设置：`~/.bamai/config.json`
- PID 与日志：`~/.bamai/bamai.pid`、`~/.bamai/bamai.log`
- 抓包文件：`~/.bamai/captures/`
- 环境变量覆盖：`BAMAI_DATA_DIR`、`BAMAI_DB_PATH`、`BAMAI_CONFIG_PATH`、`BAMAI_CAPTURES_DIR`、`BAMAI_OLLAMA_URL`

首次启动时，Bamai 会安全地把旧的 `~/.macpilot` 复制到 `~/.bamai`，并保留原目录作为备份。

## 许可证

[MIT](LICENSE) © Bamai contributors。
