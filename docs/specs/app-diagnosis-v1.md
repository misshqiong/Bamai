# 应用诊断与 SRE 工具包(phase 3)实现规格

## 背景与目标

在应用聚合视图 v1(docs/specs/app-view-v1.md,已完成)基础上,让 agent 成为"电脑运营专家":

1. **SRE 探测工具**:新增 http_timing / whois_lookup / tls_check 三个探测,升级现有 dns 探测支持多解析器对比;
2. **Agent 应用工具**:get_app_overview / get_app_history / get_app_connections 三个新工具,run_probe allowlist 扩充;
3. **诊断 SOP**:system prompt 增加应用卡顿诊断方法论;
4. **一键诊断**:应用详情页「让 AI 诊断」按钮,预置指令进入聊天。

## 现有代码基础(必须先读)

- `server/toolbox/base.py`:ProbeSpec/ProbeParam/ProbeResult 契约(param type 仅 str/int/choice);
- `server/toolbox/probes/`:每个探测一个文件,`get_spec()` 模式;`common.py` 的 `run_command`/`output_of`;`ping.py` 的 `validate_host`;
- `server/toolbox/registry.py` 的 `build_registry`;
- `server/agent/tools.py`:TOOL_DEFINITIONS、ToolExecutor、AGENT_PROBE_ALLOWLIST、`_compact`;
- `server/agent/prompts.py`:build_system_prompt;
- `server/apps.py` 与 `server/db.py` 的 `app_history`/`latest_app_connections`/`latest_app_snapshots`;
- `web/js/chat.js`(ChatController)、`web/js/app.js`(应用详情渲染)、`web/js/toolbox.js`(探测 UI 自动渲染 registry specs)、`web/js/i18n.js`;
- 测试风格:tests/test_toolbox.py、tests/test_tools.py、tests/test_agent.py、tests/test_i18n.py。

## A. 新探测(server/toolbox/probes/)

### 1. http_timing.py — HTTP 分段计时(区分 DNS 慢/连接慢/TLS 慢/服务端慢)

用系统 curl 实现:`curl -o /dev/null -s -w <format> --max-time <n> <url>`,`-w` 输出 JSON(`%{json}` 在新 curl 可用,但为兼容旧 curl 用自定义 format 串:`time_namelookup/time_connect/time_appconnect/time_starttransfer/time_total/http_code/remote_ip` 以分隔符拼接,解析函数独立可单测)。

参数:
- `url`(str, required, max 500):仅允许 `http://` 或 `https://` 开头,校验函数拒绝其他 scheme、拒绝含空白字符;
- `mode`(choice: `direct`/`proxy`/`both`,默认 `direct`):
  - direct:`--noproxy '*'`;
  - proxy:用 `scutil --proxies` 读系统 HTTP 代理(`HTTPEnable`/`HTTPProxy`/`HTTPPort`,解析函数独立可单测),读不到有效代理时返回明确错误信息(ProbeResult summary 里 `proxy_available: false`),不抛异常;
  - both:两种各跑一次,rows 两行标注 `mode`,summary 给出对比结论字段(如 `direct_total_ms`/`proxy_total_ms`)。

summary 字段:`dns_ms`、`connect_ms`、`tls_ms`(https 才有,= appconnect - connect)、`ttfb_ms`、`total_ms`、`http_code`、`remote_ip`。超时 30s。

### 2. whois_lookup.py — 域名/IP 归属(RDAP over HTTPS,不依赖 whois 命令)

用 httpx(项目已有依赖)请求 `https://rdap.org/domain/{domain}` 或 `https://rdap.org/ip/{ip}`,跟随重定向,超时 15s。参数:`query`(str, required, max 253)——自动判断是 IP(ipaddress 解析成功)还是域名(复用 validate_host)。

摘要提取函数独立可单测(输入 RDAP JSON dict):
- 域名:注册商(entities 里 role 含 registrar 的 vcard fn)、创建/过期时间(events 里 registration/expiration)、状态;
- IP:`name`、`startAddress`-`endAddress`、country、持有机构(entities vcard fn)。

网络失败/404 时 summary 返回 `found: false` 和原因,不抛异常。此探测发外网请求:httpx 用默认 trust_env(不要对外网强制 trust_env=False;项目里 trust_env=False 仅用于本机回环请求)。

### 3. tls_check.py — TLS 证书检查

纯 Python `ssl` + `socket` 实现(不用 openssl 命令):连接 `host:port`(host str required 复用 validate_host;port int 默认 443,范围 1–65535),`ssl.create_default_context()` 校验链;summary:`valid`(bool)、`protocol`(如 TLSv1.3)、`issuer`、`subject`、`not_after`(ISO 格式)、`days_remaining`、`san_count`。链校验失败时(ssl.SSLCertVerificationError)summary `valid: false` + `error` 原因,并用不校验的 context 二次连接取证书信息填充其余字段。超时 10s。日期解析函数独立可单测。

### 4. dns.py 升级 — 多解析器对比

新增参数 `resolver`(choice: `system`/`ali`/`google`/`compare`,默认 `system`;向后兼容,旧调用不传即原行为):
- system:现行 `dig +short`;
- ali:`dig +short @223.5.5.5`;google:`dig +short @8.8.8.8`;
- compare:三个都查,rows 每行带 `resolver` 字段,summary 增加 `consistent`(bool,三方 A 记录集合是否一致——只在 type=A/AAAA 时比较,其他类型 consistent 为 null)。

单个解析器超时仍 10s,compare 模式总超时 25s,单个失败不影响其他(该 resolver 的 rows 标注 error)。

### 注册与 UI

- `build_registry` 注册三个新探测(http_timing、whois_lookup、tls_check);
- toolbox 前端由 specs 自动渲染,只需在 i18n.js 补 key(中英):`toolbox.probe.http_timing.name/.desc`、`toolbox.probe.whois_lookup.*`、`toolbox.probe.tls_check.*`、新参数 label key(`toolbox.param.url`、`toolbox.param.mode`、`toolbox.param.query`、`toolbox.param.resolver` 等)。命名遵循现有 key 风格,以现有文件为准。

## B. Agent 工具(server/agent/tools.py)

TOOL_DEFINITIONS 新增:

1. `get_app_overview`:「获取按应用聚合的当前资源占用(CPU/内存/网络/进程数)」,参数 `limit`(int 1–30, required)。实现:`group_captured_processes(list_application_processes())` + `latest_app_snapshots()` 的网络列(与 /api/apps 端点同逻辑——把 main.py 里这段抽成可复用函数放 server/apps.py,端点与工具共用,避免复制);
2. `get_app_history`:「查询某应用一段时间的 CPU/内存/网络历史,用于和当前值对比找基线」,参数 `app`(string required)、`start_ts`/`end_ts`(int required)。返回 db.app_history(max_points=30)+ summary(cpu/mem 的 min/max/avg);app 无数据时返回 `{"available": false}`;
3. `get_app_connections`:「查询某应用最近的网络连接:远端域名/IP、流量、RTT、是否经本地代理」,参数 `app`(string required)。返回 latest_app_connections(limit 30),data 里附说明字段 `note`:via_proxy=1 的连接 RTT 是到本地代理的,不代表真实网络延迟;
4. `run_probe` 的 enum 与 `AGENT_PROBE_ALLOWLIST` 扩充为:现有 6 个 + `traceroute`、`netquality`、`http_timing`、`whois_lookup`、`tls_check`(capture 保持排除,它需要授权且产物大)。

三个新工具的 handler 遵循现有 `_only`/`_integer`/`_text` 校验风格,输出过 `_compact`,summary 用中文一句话。

## C. 诊断 SOP(server/agent/prompts.py)

1. `build_system_prompt` 追加一段(中文,保持现有编号风格续排):应用卡顿/占用高的诊断方法——先 get_app_overview 定位应用 → get_app_history 对比该应用自身历史基线(区分突增和一贯如此)→ 进程明细找元凶子进程 → get_app_connections 看网络质量(RTT 高/重传、注意 via_proxy 连接的 RTT 不代表真实延迟,真实延迟用 http_timing 探测)→ 需要时用探测定性(http_timing 分段、dns compare 判断解析问题、whois_lookup 识别陌生对端、tls_check 查证书)→ 结论必须包含:元凶是什么、是应用自身问题还是系统性问题、一到三条可操作建议;
2. 新函数 `build_app_diagnosis_prompt(app: str, language: str) -> str`(中英两版):作为用户消息的预置诊断指令,要求 agent 按上述 SOP 诊断指定应用当前状态。

## D. 一键诊断按钮(前端)

1. 应用详情 `panel-heading` 加按钮(id `app-diagnose`,样式复用现有按钮类,i18n key `apps.diagnose`:「让 AI 诊断」/“Ask AI to Diagnose”);
2. 点击后:把预置文本(前端用 i18n 生成用户可见短句,key `apps.diagnosePrompt`:「请诊断应用 {name} 当前的资源占用和网络状况,告诉我它是否有问题、原因和建议。」/英文对应)作为用户消息发进聊天。实现方式:`ChatController` 增加公开方法 `sendPreset(text)`(等价于用户在输入框输入后提交,复用 submit 逻辑;busy 或未 ready 时禁用按钮/给出现有样式的提示);`initChat()` 返回的 controller 需要能被 app.js 访问(app.js 与 chat 初始化的衔接方式以现有代码为准,最小改动);
3. 聊天侧栏折叠时自动展开(现有 complementary 折叠逻辑,以代码为准)。

## E. 测试(pytest + 现有风格)

- 三个新探测:参数校验(非法 scheme/host、port 范围)、输出解析函数(curl -w 格式串、scutil --proxies 输出、RDAP JSON 摘要、证书日期)全部用 fixture 单测,runner 层 mock `run_command`/httpx/socket;
- dns compare:mock run_command,验证 consistent 判定(一致/不一致/非 A 类型为 null)与单解析器失败容错;
- registry:注册数量与 id 集合断言更新;
- agent:三个新工具的 handler(mock db/psutil 层)、allowlist 与 TOOL_DEFINITIONS enum 一致性断言、非法参数报 ToolError;
- prompts:build_app_diagnosis_prompt 中英包含应用名;system prompt 包含诊断段落关键词;
- i18n:新增 key 中英完整性(沿用 test_i18n 的机制);
- 前端无测试框架,`node --check` 全部改动的 js。

## 约束

- 全部命令用参数列表执行、无 shell;外网请求仅限 whois_lookup(RDAP)与用户/agent 显式发起的 http_timing;
- 所有探测失败必须返回带原因的 ProbeResult 或 ProbeValidationError,不允许未捕获异常;
- 中文注释/日志,类型标注,ruff 通过;
- 完成后全量 pytest 通过,不破坏现有用例。
