# MacPilot（Phase 1）

MacPilot 是仅监听本机地址的 macOS 系统监控面板。当前阶段包含 CPU、内存、磁盘、网络、进程、事件规则和文件搜索；AI 对话将在 Phase 2 接入。

## 启动

```bash
./run.sh
```

首次启动会创建 `.venv`、安装 Python 依赖并下载本地 ECharts。随后访问 <http://127.0.0.1:8737>。监控数据库默认位于 `~/.macpilot/data.db`。

## 测试

```bash
.venv/bin/python -m pytest
```

