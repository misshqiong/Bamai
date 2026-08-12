# 应用历史增强(子进程维度 + 24h 时间窗)实现规格

## 背景与目标

应用详情页的 CPU/内存历史目前只有应用整体一条序列,且前端写死 1 小时窗口。本次:

1. **子进程维度**:新表按「应用 × 进程名」存历史(同名多实例求和,如 Chrome 的多个 Renderer 合为一条序列;pid 随子进程重启漂移,名称级序列才能跨 24h 稳定),详情页可选「整体」或某个子进程查看曲线;
2. **时间窗**:详情页历史支持 1h / 6h / 24h 切换;
3. agent 的 get_app_history 同步支持可选 `process_name` 参数。

## 现有代码基础(必须先读)

- `server/collector.py` 的 `_collect_apps`(每 60s 慢速 tick,手头已有 `groups`(含 pids)与 `self._latest_processes`(含每 pid 的 name/cpu/memory_rss),直接 join 即可,不要再跑 psutil);
- `server/db.py`:`app_snapshots` 的 insert/查询/`app_history` 降采样模式、`cleanup`;
- `server/main.py`:`GET /api/apps/{app}/detail`(已有 `window` 参数但前端未用);
- `server/apps.py`、`server/agent/tools.py` 的 `_app_history`;
- `web/js/app.js` 应用详情渲染与 `loadAppDetail`、`web/js/charts.js`、`web/index.html` 详情区、`web/js/i18n.js`;
- 测试:tests/test_db.py、test_api.py、test_tools.py、test_apps.py、test_i18n.py。

## 数据层

新表(沿用现有 migration 模式):

```sql
CREATE TABLE IF NOT EXISTS app_process_snapshots(
    ts INTEGER NOT NULL, app TEXT NOT NULL, name TEXT NOT NULL,
    cpu_percent REAL NOT NULL, memory_rss INTEGER NOT NULL,
    proc_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_process_snapshots_app
    ON app_process_snapshots(app, name, ts);
CREATE INDEX IF NOT EXISTS idx_app_process_snapshots_ts ON app_process_snapshots(ts);
```

- `_collect_apps` 每轮写入:对每个应用组,把组内进程按 `name` 聚合(cpu 求和、rss 求和、实例数),全部写入(不做阈值过滤,避免曲线出现误导性断档);
- 保留期:新增 `config.APP_RETENTION_SECONDS = 48 * 60 * 60`,`cleanup` 中 `app_snapshots`、`app_connections`、`app_process_snapshots` 三张表改用它(现在跟着 7 天的 process_cutoff,应用级数据只需覆盖 24h 窗口,48h 留余量即可,顺带控制库体积);
- db 新方法:
  - `app_process_history(app, name, start, end, max_points)`:与 `app_history` 相同的降采样逻辑(可把降采样抽成私有辅助函数复用,不要复制两份);
  - `app_process_names(app, start, end, limit=30)`:窗口内出现过的进程名,按 `MAX(cpu_percent)` 降序,附 `peak_cpu`、`peak_memory_rss`,供下拉框排序展示。

## API 层

- `GET /api/apps/{app}/detail`:响应新增 `process_names`(调 `app_process_names`,窗口与 `window` 参数一致);`window` 校验范围保持现状;
- 新端点 `GET /api/apps/{app}/process-history`,Query:`name`(必填,max 200)、`window`(默认 3600,ge=60、le=7*24*3600)。返回 `{"app", "name", "history": [...]}`,序列字段与 `app_snapshots` 历史一致(ts/cpu_percent/memory_rss/proc_count)。

## 前端

应用详情历史面板(`apps.history` 那个 panel)的 heading 加两个控件(样式复用仪表盘 `history-controls`/`control-row` 的既有类):

1. **时间窗**:三个按钮或 select,1h / 6h / 24h(i18n key `apps.window.1h/.6h/.24h`),默认 1h;
2. **序列选择**:select,第一项「整体」(key `apps.series.total`),其余为 `process_names`(显示名 + 实例数可省略,按返回顺序);

切换任一控件即重新拉取并重绘曲线(整体 → detail 的 history;子进程 → process-history 端点)。切换应用时序列选择重置为「整体」,时间窗保持用户上次选择。图表仍是现有 CPU/内存双序列。轮询刷新详情时保持当前选择不被重置。

### 连接表改版(同批交付)

现在连接表的目的地挤在一列(有域名只显示域名,IP 被隐藏)。改为独立列:

| 域名 | IP | 端口 | ↑↓速率 | RTT | 出口 |

- **域名**列:`domain`,为空显示「—」(key `apps.noDomain` 可复用现有占位习惯);
- **IP** / **端口**:`remote_ip`、`remote_port` 原样展示;
- **出口**列(key `apps.egress`):`via_proxy=0` 显示「直连」(key `apps.egressDirect`);`via_proxy=1` 显示「代理 · {proxy_name}」(proxy_name 为空只显示「本地代理」,复用 `apps.localProxy`)。代理场景下该行的 IP:端口本身就是本地出口地址(如 127.0.0.1:7890),无需额外字段;
- 数据全部来自现有 `connections` 响应字段,后端不改;
- i18n 新 key 中英齐全,表头 key 相应调整(`apps.destination` 拆为 `apps.domain`/`apps.ip`/`apps.port`)。

## Agent

`get_app_history` 增加可选参数 `process_name`(string):传了则查 `app_process_history`,summary 与现有格式一致并注明进程名;工具描述更新为「查询某应用（或其某个子进程）一段时间的 CPU、内存历史」。TOOL_DEFINITIONS 的 properties 同步(required 不变,仍是 app/start_ts/end_ts)。

## 测试

- db:app_process_snapshots 插入/查询/降采样/`app_process_names` 排序/cleanup 用 APP_RETENTION_SECONDS;
- collector:`_collect_apps` 写入按名聚合的行(mock db,复用现有 collector 测试的方式);
- API:detail 含 process_names;process-history 正常与参数校验(缺 name、window 越界);
- tools:get_app_history 带/不带 process_name 两条路径;
- i18n:新 key 中英完整;
- 全量 pytest + ruff + node --check 通过。

## 约束

- 中文注释、类型标注、风格与仓库一致;降采样逻辑复用不复制;不改动现有端点的响应字段语义(只增不改)。
