# 应用聚合视图 v1(Tier 0)实现规格

## 背景与目标

当前产品只能看整机 CPU/内存和零散的进程列表,无法回答"某个应用为什么卡"。本版本(Tier 0,零权限、全 mac 通用)交付:

1. **进程 → 应用归组**:把 Chrome 主进程 + 十几个 Helper 聚合成一个"Google Chrome";
2. **应用聚合 API 与前端「应用」视图**:按应用看 CPU/内存/网络/进程数,可下钻详情;
3. **应用级历史**:新表 `app_snapshots`,支持"该应用过去一小时的曲线";
4. **连接级网络采集**:nettop 连接级(去掉 `-P`),拿到每应用每连接的远端 IP:端口、流量、TCP RTT、重传;反查 DNS(带缓存)做域名标注;识别"经本地代理"的连接。

明确 **不做**(后续版本):agent 新工具、SRE 探测(whois/http_timing 等)、DNS/SNI 嗅探、代理软件 API 适配、ASN/RDAP 外部查询(v1 不发任何外网请求)。

## 现有代码基础(必须先读)

- `server/collector.py`:采集线程,60s 慢速 tick 里已有进程快照(`_collect_processes`)和 nettop 进程级流量(`_collect_process_net`,`parse_nettop_output`);
- `server/db.py`:SQLite,`init_schema` 用 `CREATE TABLE IF NOT EXISTS` + 存量表 `ALTER TABLE ADD COLUMN` 的迁移模式,`cleanup` 做保留期清理;
- `server/main.py`:FastAPI,现有 `/api/processes`、`/api/metrics` 等;
- `web/index.html` + `web/js/app.js`:tab 用 `<button class="tab" data-view="xxx">` + `<section id="xxx-view" class="view">`;i18n 用 `data-i18n` 属性,文案在 `web/js/i18n.js`(中英都要加);图表用 `web/js/charts.js`(ECharts,本地 vendor);
- 测试风格参考 `tests/test_api.py`、`tests/test_db.py`、`tests/test_tools.py`。

## 模块设计

### 1. 归组模块 `server/apps.py`(新文件,纯函数为主,可单测)

```python
def extract_app_from_path(path: str) -> str | None: ...
def group_processes(procs: list[dict]) -> list[dict]: ...
```

归组规则(按顺序):

1. 从进程 exe/cmdline 第一段提取**最外层** `.app` bundle:
   `/Applications/Google Chrome.app/Contents/Frameworks/...Helper.app/...` → `Google Chrome`(取最外层,不是最内层 Helper.app;去掉 `.app` 后缀)。
2. 提取不到的,沿 ppid 父链向上找(psutil `Process.parent()`,最多 5 层,注意 NoSuchProcess),父链上某进程能提取出 app 的,归入该 app。
3. 仍然没有的,归为后台组:`app = 进程名`,标记 `kind="background"`;app 进程标记 `kind="app"`。

聚合字段:`app, kind, cpu_percent(求和), memory_rss(求和), proc_count, pids(列表)`。

`group_processes` 输入为已含 `pid/name/cpu_percent/memory_rss/cmdline/exe` 的字典列表,不在函数内部调 psutil(便于单测);父链查询通过可注入的 `parent_lookup: Callable[[int], int | None]` 参数实现,默认用 psutil。

### 2. 连接级网络 `server/collector.py`

把 `_collect_process_net` 改为连接级:`nettop -L 1 -x -J bytes_in,bytes_out,rtt_avg`(去掉 `-P`)。

**实现前必须实际运行该命令确认输出格式**(你在 mac 上,可直接跑),已知要点:CSV 中进程行的第一列是 `name.pid`,其下的连接行第一列形如 `tcp4 10.0.0.5:52344<->93.184.216.34:443`,解析时用"最近一次出现的进程行"作为连接的归属进程。rtt_avg 单位需实测确认(疑似毫秒,udp 行无 rtt)。若某列在部分系统缺失,解析要容错(参考现有 `parse_nettop_output` 对无表头的兜底)。

- 进程级流量逻辑保留(`process_net` 表和现有 API 不动):可从连接级数据按进程求和得出,替代原 `-P` 调用,避免跑两次 nettop;
- 连接级速率同样用"与上次采样的累计字节差 / 时间差"计算,key 为 `(pid, 连接四元组)`;
- **本地代理识别**:远端 IP 是回环(`127.0.0.0/8`、`::1`)的连接标记 `via_proxy=1`,并尽力标注代理进程名(对该回环端口,在本次 nettop 结果里找监听/对应的进程;找不到就留空,不额外调 lsof);
- **反查 DNS**:独立的 `ReverseDnsCache` 类(可单测):`socket.getnameinfo` 反查,LRU + TTL(1 小时,含负缓存),由单独的 daemon 线程消费队列解析,采集线程只做 cache 查询和入队,**绝不阻塞**;每轮新入队 IP 上限 20 个;回环/内网 IP 不反查。查到的域名写入当轮及后续采样。

### 3. 数据层 `server/db.py`

新表(沿用现有 migration 模式,`init_schema` 里 `CREATE TABLE IF NOT EXISTS`):

```sql
CREATE TABLE IF NOT EXISTS app_snapshots(
    ts INTEGER NOT NULL, app TEXT NOT NULL, kind TEXT NOT NULL,
    cpu_percent REAL NOT NULL, memory_rss INTEGER NOT NULL,
    proc_count INTEGER NOT NULL, up_bps REAL NOT NULL, down_bps REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_snapshots_ts ON app_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_app_snapshots_app ON app_snapshots(app, ts);
CREATE TABLE IF NOT EXISTS app_connections(
    ts INTEGER NOT NULL, app TEXT NOT NULL, remote_ip TEXT NOT NULL,
    remote_port INTEGER NOT NULL, domain TEXT, proto TEXT NOT NULL,
    up_bps REAL NOT NULL, down_bps REAL NOT NULL,
    rtt_ms REAL, via_proxy INTEGER NOT NULL DEFAULT 0, proxy_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_app_connections_ts ON app_connections(ts);
CREATE INDEX IF NOT EXISTS idx_app_connections_app ON app_connections(app, ts);
```

写入时机:与现有慢速 tick(60s)相同。`app_snapshots` 每轮写全部应用组;`app_connections` 同一 `(app, remote_ip, remote_port)` 每轮合并成一行(多条连接求和流量、RTT 取加权平均)。两表都进 `cleanup` 保留期(与 `process_snapshots` 一致)。

新增查询方法:`latest_app_snapshots()`、`app_history(app, start, end)`(降采样逻辑可参考 `query_metrics`)、`latest_app_connections(app, limit=50)`(按流量降序)、`app_processes_latest(app)`(该 app 最近一轮的进程明细,join process_snapshots 需要在 app_snapshots 里……为免复杂化:进程明细走实时 psutil,见 API 层)。

### 4. API 层 `server/main.py`

- `GET /api/apps?sort=cpu|memory|network&limit=30`:**实时**计算——psutil 全进程列表 → `group_processes` 聚合,网络列 join 数据库中最近一轮连接级数据(按 app 求和)。返回 `{"apps": [{app, kind, cpu_percent, memory_rss, proc_count, up_bps, down_bps}]}`;
- `GET /api/apps/{app}/detail?window=3600`:返回 `{"app", "processes": [实时子进程明细], "history": [app_snapshots 序列], "connections": [最近的连接聚合: domain/remote_ip/port/up_bps/down_bps/rtt_ms/via_proxy/proxy_name]}`。app 名走 URL path 参数,注意含空格的名字("Google Chrome")的编码;
- 参数校验风格与现有端点一致(Query + ge/le)。

### 5. 前端 `web/`

- `index.html`:在「仪表盘」后加 tab `<button class="tab" data-view="apps" data-i18n="nav.apps">应用</button>` 和对应 `<section id="apps-view" class="view">`;
- 列表:表格列 = 应用名(kind=background 的加灰色"后台"徽标)、CPU%、内存、网络↑↓、进程数;默认按 CPU 降序,表头可切换排序(重新请求带 sort 参数即可);视图激活时每 5s 轮询,离开视图停止(参考 app.js 现有轮询模式);
- 点击行展开详情区(同一视图内,上方列表下方详情即可,不做路由):ECharts 双曲线(CPU%、内存,时间窗 1h)+ 子进程表(名称/PID/CPU/内存)+ 连接表(域名或 IP:端口、↑↓速率、RTT、代理标记);域名列有 domain 显示 domain,否则显示 `IP:port`,并在连接表标题处标注"基础模式 · IP 级,部分域名经反查获得";
- `i18n.js` 中英文案都要补全;样式沿用现有 panel/table 类,不引入新依赖。

### 6. 测试(pytest,风格与现有一致)

- `tests/test_apps.py`:`extract_app_from_path` 覆盖:普通 app、嵌套 Helper.app、非 app 路径、空串、`/System/Applications/...`;`group_processes` 覆盖父链归组与后台组;
- nettop 连接级解析:用固定 fixture 字符串测(进程行+连接行混合、缺 rtt 列、udp 行、无表头);
- `ReverseDnsCache`:TTL、负缓存、队列上限(mock socket);
- DB 新表插入/查询/cleanup;
- 两个新 API 端点(TestClient,mock psutil 层可参考现有测试对 collector 的处理)。

## 约束

- 全部代码遵循仓库现有风格:中文注释/日志、类型标注、`_safe` 隔离采集异常;
- v1 不发任何外网请求;所有采集失败必须不影响主流程(单项 `_safe`);
- nettop 每轮仍只调用一次;
- 完成后跑全量 `pytest`,不允许破坏现有用例。
