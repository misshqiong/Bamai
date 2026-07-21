# MacPilot — 本地 AI 系统监控助手 技术规格

一个运行在 macOS 上的本地系统监控应用：实时监控 CPU / 内存 / 存储 / 网络，
并通过本地 LLM（Ollama + Qwen3 4B）提供自然语言问答、智能诊断和搜索能力。
全程离线，数据不出本机。

## 0. 总体约束

- 目标平台：macOS (Apple Silicon)，Python 3.12+（本机为 3.14.4，位于 /opt/homebrew/bin/python3）
- 前端：**无构建链**。纯 HTML + ES Modules + CSS，图表用 ECharts（vendor 到本地，见 §6）
- 后端：Python + FastAPI + uvicorn
- 存储：SQLite（stdlib sqlite3 即可，注意多线程访问方式）
- LLM：Ollama HTTP API（http://localhost:11434），模型 `qwen3:4b`；embedding 暂不使用
- **没有 Ollama 时应用必须正常运行**（监控功能不受影响，聊天界面显示引导安装提示）
- 依赖尽量少：fastapi、uvicorn、psutil、httpx、pydantic 为主，避免重型依赖
- 代码注释与 UI 文案使用中文；代码标识符使用英文

## 1. 项目结构

```
ai_local/
├── SPEC.md
├── README.md            # 安装、启动、使用说明（中文）
├── requirements.txt
├── run.sh               # 一键启动脚本（创建 venv、装依赖、下载 vendor、启动 uvicorn）
├── server/
│   ├── __init__.py
│   ├── main.py          # FastAPI app：路由、WebSocket、静态文件挂载、生命周期管理
│   ├── config.py        # 常量配置（采样间隔、保留时长、阈值等）
│   ├── db.py            # SQLite 封装：建表、写入、查询、降采样、滚动清理
│   ├── collector.py     # 后台采集线程：psutil + nettop
│   ├── rules.py         # 规则引擎：阈值检测 → events 表 + 触发 AI 诊断
│   ├── notify.py        # macOS 系统通知（osascript display notification）
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── ollama_client.py   # Ollama HTTP 封装：health、chat（含 tool calling 循环）
│   │   ├── tools.py           # 工具定义（JSON schema）+ 工具执行分发
│   │   └── prompts.py         # 系统提示词
│   └── search/
│       ├── __init__.py
│       └── files.py     # 文件搜索：mdfind 封装 + 大文件/大目录扫描
├── web/
│   ├── index.html       # 单页应用：仪表盘 + 聊天侧栏
│   ├── css/app.css
│   ├── js/
│   │   ├── app.js       # 入口：WebSocket 连接、路由各面板
│   │   ├── charts.js    # ECharts 实时曲线封装
│   │   ├── chat.js      # 聊天界面逻辑
│   │   └── api.js       # REST 封装
│   └── vendor/
│       └── echarts.min.js   # 由 run.sh 首次启动时下载（见 §6）
└── tests/
    ├── test_db.py
    ├── test_rules.py
    └── test_tools.py
```

## 2. 数据采集（collector.py）

独立后台线程（daemon thread），主循环每 **3 秒**一个 tick：

每 tick 采集并写入 `metrics` 表：
- CPU：`psutil.cpu_percent(percpu=True)` 总体 + 每核；`os.getloadavg()`
- 内存：`psutil.virtual_memory()`（total/used/available/percent）、`psutil.swap_memory()`
- 磁盘 I/O：`psutil.disk_io_counters()` 差分得出读写速率 (bytes/s)
- 网络：`psutil.net_io_counters()` 差分得出上下行速率 (bytes/s)

每 **60 秒**（每 20 个 tick）额外执行：
- 进程快照 → `process_snapshots` 表：Top 15 进程（按 CPU 和按内存各取 top，去重合并），
  字段：pid、name、cpu_percent、memory_rss、cmdline(截断 200 字符)
- 磁盘容量 → `disk_usage` 表：对 `/` 和 `/System/Volumes/Data` 等挂载点 `psutil.disk_usage()`
- 每进程网络流量：解析 `nettop -P -L 1 -x -J bytes_in,bytes_out` 输出 → `process_net` 表。
  nettop 输出为 CSV 格式，进程列形如 `name.pid`。**必须容错**：nettop 失败/超时(5s)时跳过，
  不影响主循环。差分计算每进程速率。
- 调用 `rules.evaluate()` 做异常检测

采集异常必须被捕获并记录日志，任何单项失败不能中断采集线程。

## 3. 存储设计（db.py）

SQLite 文件：`~/.macpilot/data.db`（目录不存在则创建）。
WAL 模式。采集线程独占写连接；FastAPI 请求使用只读连接（`check_same_thread=False` 或每请求新建）。

表结构：

```sql
metrics(ts INTEGER, cpu_percent REAL, cpu_per_core TEXT/*json*/, load_1 REAL,
        mem_used INTEGER, mem_total INTEGER, mem_percent REAL, swap_used INTEGER,
        disk_read_bps REAL, disk_write_bps REAL, net_up_bps REAL, net_down_bps REAL)
metrics_hourly(hour_ts INTEGER PRIMARY KEY, /* 各指标的 avg/max */ ...)
process_snapshots(ts INTEGER, pid INTEGER, name TEXT, cpu_percent REAL,
                  memory_rss INTEGER, cmdline TEXT)
process_net(ts INTEGER, pid INTEGER, name TEXT, up_bps REAL, down_bps REAL)
disk_usage(ts INTEGER, mount TEXT, total INTEGER, used INTEGER, percent REAL)
events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, severity TEXT,
       title TEXT, detail TEXT, ai_analysis TEXT, resolved_ts INTEGER)
```

数据保留策略（每小时执行一次清理）：
- `metrics`：保留 48 小时；删除前先把整点小时聚合进 `metrics_hourly`（永久保留）
- `process_snapshots` / `process_net`：保留 7 天
- `disk_usage`：保留 90 天
- `events`：保留 90 天

查询接口需提供**降采样**：请求任意时间范围时，返回不超过 ~500 个点
（按时间分桶取 avg/max），供前端画图和 LLM 工具调用共用。

## 4. REST / WebSocket API（main.py）

- `GET /` → web/index.html；`/static/*` → web 目录
- `WS /ws/realtime`：每 3 秒推送最新一条 metrics（JSON），供仪表盘实时刷新
- `GET /api/overview`：当前快照（最新 metrics + 磁盘容量 + Top5 进程 + 未解决 events 数 + ollama 状态）
- `GET /api/metrics?metric=&start=&end=`：历史序列（降采样后）
- `GET /api/processes?sort=cpu|memory&limit=20`：实时进程列表（psutil 现查）
- `GET /api/processes/net`：最近一次每进程网络流量
- `GET /api/events?limit=50`：事件列表
- `POST /api/chat`：`{messages: [...]}` → 执行 agent 工具循环 → 返回
  `{reply, tool_trace: [{tool, args, summary}]}`。tool_trace 用于前端展示"查了什么"。
  超时 120s。Ollama 不可用时返回 503 + 引导信息。
- `GET /api/ollama/status`：`{available, model_pulled, model}`
- `GET /api/search/files?q=&kind=name|content`：mdfind 文件搜索
- `GET /api/search/large-files?path=~&min_mb=100&limit=50`：大文件扫描

## 5. AI Agent（agent/）

### 对话循环（ollama_client.py）

标准 tool-calling 循环，非流式：

1. `POST /api/chat`（Ollama），带 system prompt + 历史消息 + tools 定义，`stream: false`
2. 若响应含 `tool_calls`：逐个执行（tools.py 分发），把结果以 `role: "tool"` 消息追加，回到 1
3. 最多 **6 轮**工具调用，超过则强制让模型直接作答
4. `options: {temperature: 0.7, num_ctx: 8192}`
5. Qwen3 是 thinking 模型：请求加 `think: false`（Ollama 支持）以降低延迟；
   若返回中仍有 `<think>...</think>` 内容需剥离后再返回给前端

### 工具集（tools.py）

每个工具返回**紧凑 JSON**（数字取整、序列降采样到 ≤50 点），避免撑爆 4B 模型上下文：

| 工具 | 参数 | 说明 |
|------|------|------|
| `get_current_stats` | – | 当前 CPU/内存/磁盘/网络快照 |
| `get_top_processes` | sort_by(cpu\|memory\|network), limit≤15 | 当前 Top 进程 |
| `query_metrics` | metric, start_ts, end_ts | 历史序列（≤50 点）+ min/max/avg 摘要 |
| `get_process_history` | start_ts, end_ts, name?(模糊) | 时段内进程快照聚合（谁在吃资源）|
| `get_events` | limit, since_ts? | 告警事件 |
| `find_large_files` | path, min_mb, limit≤30 | 大文件扫描 |
| `search_files` | query, kind(name\|content), limit≤20 | mdfind 搜索 |
| `search_processes` | keyword | 按名称/命令行模糊匹配运行中的进程 |

时间参数：工具接受 unix 秒；system prompt 中注入当前时间与今日 0 点时间戳，
并指示模型自行换算"昨天下午"这类相对时间。

### System Prompt（prompts.py）

要点：中文回答；你是 macOS 系统状态助手 MacPilot；回答必须基于工具返回的真实数据，
数字不得编造；数据不足时明确说明；回答简洁、给出可操作建议；
杀进程等危险操作只能建议、由用户自己执行。

## 6. 前端（web/）

单页布局：左侧主区仪表盘，右侧固定聊天栏（可折叠）。深色主题。

仪表盘组件：
1. **顶部状态卡**：CPU%、内存%、磁盘使用%、网络↑↓速率，4 张卡片实时刷新
2. **实时曲线**（ECharts，滚动窗口 5 分钟，WebSocket 驱动）：CPU、内存、网络、磁盘 I/O
3. **历史视图**：时间范围切换（1h / 6h / 24h / 7d），调 `/api/metrics`
4. **进程表**：Top 进程（CPU/内存/网络 三种排序切换），每 5s 刷新
5. **事件面板**：告警列表，有 AI 诊断的可展开查看
6. **搜索页签**：文件名/内容搜索框（mdfind）+ 大文件扫描（选路径、按大小排序）

聊天栏：消息列表 + 输入框；助手消息下方以折叠小字展示 tool_trace
（如"🔧 查询了 14:00–18:00 的内存数据"）；Ollama 未就绪时显示安装引导
（brew install ollama / ollama pull qwen3:4b）与"重新检测"按钮。

ECharts vendor：run.sh 首次运行时若 `web/vendor/echarts.min.js` 不存在，
从 `https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js` 下载。
index.html 只引用本地 vendor 文件。

## 7. 规则引擎（rules.py）

每 60s 评估，触发时写 `events` 并调用 notify.py 发 macOS 通知
（`osascript -e 'display notification ...'`）。同类事件 30 分钟内去重。
条件恢复后自动写 resolved_ts。

| 规则 | 条件（基于最近 N 分钟 metrics） | severity |
|------|------|------|
| cpu_high | CPU 均值 > 85% 持续 3 分钟 | warning |
| mem_pressure | 内存 > 90% 或 swap 增长超过 2GB/10min | warning |
| disk_full | 任一挂载点 > 90%（>95% 为 critical）| warning/critical |
| net_spike | 网络速率超过过去 1h 均值 10 倍且 > 10MB/s，持续 2 分钟 | info |

事件生成后，若 Ollama 可用，异步调用 agent（复用工具循环，输入为事件上下文）
生成诊断写入 `ai_analysis`；不可用则留空。AI 诊断失败不影响事件本身。

## 8. 文件搜索（search/files.py）

- `mdfind_search(query, kind)`：kind=name → `mdfind -name <q>`；kind=content → `mdfind <q>`。
  限制结果数、超时 10s、过滤系统目录（/System、/Library 顶层）。
- `find_large_files(path, min_mb, limit)`：`os.walk` 扫描（跳过隐藏目录与包目录 .app/.framework 内部、
  拒绝越出用户目录之外的路径除非显式指定）、按大小排序。单次扫描超时 30s，超时返回已扫到的结果并标注截断。

## 9. 启动与运维

- `run.sh`：`python3 -m venv .venv` → pip install → 下载 vendor → `uvicorn server.main:app --host 127.0.0.1 --port 8737`
- 只监听 127.0.0.1
- 启动时打印访问地址；日志用 logging，INFO 级别，采集错误 WARNING
- `config.py` 集中所有可调参数

## 10. 测试与验收

pytest 单测（不依赖 Ollama、不依赖真实系统状态）：
- test_db：写入/查询/降采样/清理逻辑（用临时 DB 文件 + 伪造时间戳）
- test_rules：各规则触发/去重/恢复（注入伪造 metrics）
- test_tools：工具分发、参数校验、紧凑化输出（mock db）；nettop 输出解析（用固定样例文本）

验收清单（人工）：
1. `./run.sh` 一次成功启动，浏览器打开 http://127.0.0.1:8737 仪表盘正常
2. 实时曲线 3s 刷新；历史视图切换正常；进程表排序正常
3. 无 Ollama 时聊天栏显示引导而非报错
4. 文件搜索与大文件扫描可用
5. pytest 全绿

## 11. 实现阶段划分

- **Phase 1（M1）**：collector、db、rules(仅事件不含 AI 诊断)、notify、REST/WS、完整仪表盘前端、
  搜索后端+前端、run.sh、tests。聊天栏 UI 占位（显示"AI 未接入"）。
- **Phase 2（M2/M3）**：agent 全套、/api/chat、聊天前端逻辑、事件 AI 诊断、README 完善。
