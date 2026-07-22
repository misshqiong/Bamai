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
- **Phase 3（开源化）**：§12–§18，分四个任务：
  - **P3-A**：§12 改名 Bamai + §14 双语 i18n 框架 + §13 健康横幅
  - **P3-B**：§15 bamai CLI + §16 运行时设置与模型切换
  - **P3-C**：§17 工具箱框架 + 9 个探测工具（含抓包）
  - **P3-D**：§18 开源整备

---

# Phase 3 规格：开源化改造

## 12. 项目改名：MacPilot → Bamai（把脉）

- 应用名 `Bamai`，中文名"把脉"，tagline：*Take your Mac's pulse, locally.* / 给你的 Mac 把把脉。
- 全局替换：`config.APP_NAME`、UI 标题与文案、日志、系统提示词中的自称、README、注释。
- 数据目录 `~/.macpilot` → `~/.bamai`；启动时若旧目录存在且新目录不存在，自动整体重命名迁移并打日志。
- 环境变量前缀 `MACPILOT_*` → `BAMAI_*`（预发布阶段，直接改，无需兼容旧名）。
- 端口不变 (8737)。`run.sh` 保留为 `./bamai start --foreground` 的一行兼容包装。
- Agent 系统提示词自称改为 Bamai（把脉）。

## 13. 系统健康横幅（仪表盘最顶部，小白优先）

新端点 `GET /api/health`（纯规则计算，不调用 LLM，毫秒级返回）：

```json
{"level": "ok|warn|critical", "checks": [
  {"kind": "memory", "level": "warn", "params": {"percent": 82, "top_name": "Chrome", "top_gb": 6.2}},
  ...
], "ai_analysis": "最近未解决事件的 AI 诊断文本或 null"}
```

- level 聚合规则：任一 critical → critical；任一 warn → warn；否则 ok。
- checks 覆盖：CPU、内存、磁盘容量、网络异常、swap，复用 rules.py 的窗口逻辑但独立于事件去重
  （横幅反映当下，事件表反映历史）。
- **文案模板在前端 i18n 字典中**，后端只返回 kind + params。每个 kind 两条模板：
  `headline`（一句话结论）和 `advice`（一步可执行的建议），措辞禁止术语
  （不出现 swap/RSS/bps；说"电脑可能变卡"而非"内存压力高"）。
- 前端横幅：绿/黄/红大色块 + headline + advice；有多条异常时列表展示；
  `ai_analysis` 存在时直接显示在横幅内（替代模板 advice），并标注"AI 分析"。
- **[AI 解读] 按钮** → `POST /api/health/explain`：复用 agent 工具循环，输入为当前 health JSON，
  产出一段面向小白的当前状态体检报告（语言跟随 §14 的界面语言）；30s 超时；
  Ollama 未就绪时按钮置灰。生成结果缓存在前端，不入库。
- 事件的 AI 诊断（EventDiagnoser）提示词同步改写为小白友好措辞。

## 14. 双语 i18n（中文 / English）

- **前端**：`web/js/i18n.js` 导出字典 `{zh: {...}, en: {...}}` 与 `t(key, params)`；
  静态文案用 `data-i18n` 属性 + 初始化扫描替换，动态文案统一走 `t()`。
  语言选择：localStorage 持久化，默认按 `navigator.language`（zh* → 中文，否则英文）；
  头部放 `中/EN` 切换按钮，切换即时生效无需刷新。
- **后端产生的文本**：
  - 事件表新增列 `params TEXT`（JSON）；rules.py 写事件时同时存 kind+params，
    title/detail 继续保留（按生成时语言渲染，用于通知与旧数据兼容）；前端事件面板优先用
    kind+params 走 i18n 渲染，无 params 的旧事件回退显示存量文本。
  - macOS 通知按当前设置语言渲染。
  - AI 回答语言：系统提示词注入"使用{设置语言}回答；若用户明显使用另一种语言提问，跟随用户"。
- 数字/时间格式化用 `Intl`，随语言切换。

## 15. `bamai` CLI（替代 run.sh 成为唯一入口）

项目根下可执行 bash 脚本 `./bamai`：

- `start`：venv 创建 + 依赖安装 + vendor 下载（幂等；依赖以 `requirements.txt` 的
  SHA-256 指纹缓存于 `.venv/.deps-stamp`，未变化则跳过 pip）→ 以守护方式启动 uvicorn
  （nohup，pid 写 `~/.bamai/bamai.pid`，日志 `~/.bamai/bamai.log`）→ `open http://127.0.0.1:8737`。
  已在运行则提示并直接打开浏览器。`--foreground` 前台运行（开发用）。
  AI 默认自动管理（`--with-ai` 仅为兼容保留）：已安装 ollama 时确保其运行——brew 安装的
  注册为 `brew services` 登录服务（常驻自愈），否则 nohup 兜底；未安装时交互式询问是否
  代为安装；模型缺失只提示，下载交给服务端启动时的自动后台拉取（避免重复下载）。
  服务端 lifespan：Ollama 可达且配置模型缺失时自动调用 ModelPullManager 后台下载
  （`BAMAI_AUTO_PULL=0` 关闭；测试经 collector_enabled=False 天然豁免）。
  所有对 Ollama 的 httpx 请求 `trust_env=False`（本地请求绝不走系统/环境代理——macOS 上
  httpx 会经 urllib 读到系统代理，launchd 进程无 NO_PROXY 时 loopback 请求会被代理吃掉
  返回 502）；默认地址用 `127.0.0.1:11434` 而非 localhost，避免 IPv6 歧义。
- `stop`：读 pid 优雅终止；`restart`；`status`：进程/端口/Ollama/模型四项状态；`logs`：tail -f。
- `model list|use <name>|pull <name>`：调 `/api/settings` 与 Ollama API 的薄封装。
- `autostart on|off`：生成/移除 `~/Library/LaunchAgents/com.bamai.app.plist`；
  plist 含 `KeepAlive.SuccessfulExit=false` + `ThrottleInterval=10`，异常退出自动拉起。
  项目位于 TCC 保护目录（~/Documents 等）时 launchd 读不到脚本（退出码 126），
  `on` 时打印提示建议改用 menubar。
- `menubar on|off`：用 swiftc 将 `menubar/BamaiMenuBar.swift` 构建为
  `menubar/build/Bamai.app`（LSUIElement，项目路径写入 Info.plist 的 `BamaiProjectDir`），
  生成/移除 `~/Library/LaunchAgents/com.bamai.menubar.plist`。应用每 5 秒轮询
  `/api/health`，圆点按 ok/warn/critical/不可达 显示绿/黄/红/灰；菜单提供打开控制台、
  启动/重启/停止（调用 `./bamai`）；文案按 `~/.bamai/config.json` 的 language 双语切换。
  应用同时是服务监工：连续两次轮询不可达且用户未手动停止时执行 `./bamai start`
  自动拉起（之后退避为约每分钟一次）；项目在 TCC 保护目录时依赖应用的一次性文件夹授权。
- `raycast/` 目录提供 Script Commands（Open Bamai / Bamai Status / Restart Bamai），
  用户在 Raycast 中添加该目录后可绑定全局快捷键。
- 所有输出双语（简单做法：中英并排一行，如 "已启动 / started"）。

## 16. 运行时设置与模型切换

- `~/.bamai/config.json`：`{"model", "temperature", "num_ctx", "language", "max_tool_rounds"}`；
  config.py 启动读取，缺项用默认值；写入原子化（临时文件 + rename）。
- `GET /api/settings` / `POST /api/settings`（校验字段与取值范围；model 仅接受已安装模型）。
  OllamaClient 每次请求读当前设置，切模型即时生效、无需重启。
- `GET /api/ollama/models`：已安装模型（含大小）+ 推荐列表
  `qwen3:4b (~3GB) / qwen3:8b (~6GB) / qwen3:14b (~10GB)` 及内存建议。
- `POST /api/ollama/pull {model}`：后台线程调 Ollama pull API（流式），进度存内存；
  `GET /api/ollama/pull/status` 轮询 `{model, status, percent}`；同一时刻只允许一个拉取任务。
- **前端新增"设置 / Settings"页签**：模型单选列表（已装可选、未装显示下载按钮+进度条）、
  语言、温度（0–1 滑块）、上下文长度；保存即生效，切模型后聊天栏状态标签同步更新。

## 17. 工具箱（Toolbox）：插件化探测工具框架

### 框架

- `server/toolbox/`：`base.py`（`ProbeSpec`：id、name/desc 的 i18n key、params 定义
  `[{name,type(str|int|choice),label_key,default,required,min,max,choices}]`、timeout、
  needs_authorization、runner 函数签名 `run(params) -> ProbeResult`）；
  `registry.py`（注册表 + 按 id 查找）；`probes/` 每工具一个文件。
- `ProbeResult`：`{summary: dict, rows: list[dict]|None, raw_output: str(截断8KB), artifacts: [...]}`。
- API：
  - `GET /api/toolbox`：所有工具的 spec（含授权状态）。
  - `POST /api/toolbox/{id}/run`：参数校验后启动后台 job，返回 `{job_id}`。
  - `GET /api/toolbox/jobs/{job_id}`：`{status: running|done|error, result?, error?}`；
    job 存内存，保留最近 50 个；并发上限 3 个 job。
  - `POST /api/toolbox/{id}/explain`：把最近一次结果交给 agent 生成小白解读（语言随设置）。
- 前端"工具箱 / Toolbox"页签：卡片网格（图标+名称+一句话说明）→ 点开参数表单（按 spec 自动生成）
  → 运行中转圈 → 结构化结果（summary 键值 + rows 表格）+ 折叠的原始输出 + [AI 解读] 按钮。
- **Agent 集成**：新增 agent 工具 `run_probe(probe_id, params)`，白名单仅含免授权、
  运行时间 ≤35s 的工具（ping/dns/port/wifi/battery/memory_check），参数上限同前端；
  聊天中即可"帮我 ping 一下 github.com"。抓包与 networkQuality 不进 agent（慢/需授权）。

### 首批工具（macOS 内置命令，零安装）

| id | 命令 | 参数 | summary 关键字段 |
|----|------|------|------|
| ping | `ping -c N` | host, count≤10 | 平均/最小/最大延迟、丢包率 |
| traceroute | `traceroute -m 20 -w 2` | host | hops 表（序号/IP/延迟）|
| dns | `dig +short` 各类型 | domain, type(A/AAAA/CNAME/MX/TXT) | 记录列表、耗时 |
| port | `nc -z -G 5` | host, port(1-65535) | open/closed、耗时 |
| netquality | `networkQuality -v`（macOS 12+）| – | 上/下行 Mbps、RPM 响应度 |
| memory_check | `memory_pressure` + `vm_stat` + db 进程快照对比 | window_min(5-60) | 压力等级、可回收内存、RSS 持续增长 Top5（泄漏嫌疑）|
| wifi | `system_profiler SPAirPortDataType -json` | – | SSID、信道、RSSI/噪声、协商速率 |
| battery | `pmset -g batt` + `system_profiler SPPowerDataType -json` | – | 循环次数、健康状态、当前电量 |
| capture | `tcpdump` | interface(下拉,来自`tcpdump -D`), filter(BPF表达式,校验字符集`[\w\s\.\:\-\(\)&|!]`), duration≤60, max_packets≤2000 | 见下 |

主机名/域名参数校验：`[A-Za-z0-9\.\-\:]`，长度 ≤253，禁止空白与 shell 元字符；
所有命令一律 `subprocess.run(list)` 形式，禁止 shell=True。

### 抓包（capture）专项

- snaplen 默认 96 字节（只抓包头），`-nn` 不做反解；输出解析为：协议分布、Top 会话
 （五元组聚合流量/包数）、时间范围；完整 pcap 落 `~/.bamai/captures/<ts>.pcap`，
  提供 `GET /api/toolbox/captures/{name}` 下载（仅限该目录、校验文件名）。
- **授权检测**：启动 job 前检查 `/dev/bpf0` 可读；不可读 → 返回 `needs_auth`，
  前端展示引导卡片：运行 `sudo ./scripts/enable-capture.sh`（创建 `access_bpf` 组、
  将当前用户加入、安装开机 LaunchDaemon 将 `/dev/bpf*` chgrp 到该组并 0640 —— Wireshark 同款方案；
  脚本内容含中英注释与撤销说明），配套 `scripts/disable-capture.sh` 完整回滚。
- 抓包数据只落本地文件，不入 SQLite；captures 目录进 .gitignore；文档明确安全边界。

## 18. 开源整备

- `LICENSE`：MIT，版权行 "Bamai contributors"。
- `README.md`（英文，主）+ `README.zh-CN.md`（中文）：定位一句话 + 隐私卖点前置
 （local-only、127.0.0.1、no cloud）、功能列表、Quickstart（`git clone && ./bamai start`）、
  模型推荐表、工具箱说明、抓包授权说明、架构简图、开发/测试指引。占位截图链接 `docs/screenshots/`。
- `CONTRIBUTING.md`：双语简版（如何跑测试、如何加一个 probe、代码风格）。
- `pyproject.toml`：仅 ruff 配置（line-length 100，规则 E/F/I/W）；全库 ruff 通过。
- `.github/workflows/ci.yml`：macos-latest，Python 3.12，`pip install -r requirements.txt pytest ruff`
  → `ruff check` → `pytest`。
- 清理：文档与代码中不得出现个人绝对路径（`/Users/<username>/...`）与个人信息；
  `.gitignore` 补充 `~` 类数据外的 `captures/`、`.ruff_cache/`。
- 测试补充：i18n 字典键完整性（zh/en 键集合一致）、/api/health、/api/settings、
  toolbox 框架（mock runner）、各 probe 的输出解析（固定样例文本）、CLI 语法 `bash -n`。
