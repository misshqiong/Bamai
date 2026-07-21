# Bamai

**A local-first Mac health monitor and diagnostic assistant that helps you understand what your Mac is doing.**

[中文说明](README.zh-CN.md)

## Private by design

Bamai is local-only by default:

- The web server listens only on `127.0.0.1`; it is not exposed to your network.
- Metrics, alerts, settings, and packet captures stay under `~/.bamai/` on your Mac.
- AI requests go only to your local Ollama server. Bamai has no cloud backend, telemetry, or account system.
- Monitoring and diagnostics continue to work when Ollama is not installed.

Packet captures can still contain local addresses, ports, and protocol metadata. Bamai limits capture duration and packet size, but you remain responsible for the resulting local files.

## Features

- Live CPU, per-core CPU, memory, disk I/O, network, and process monitoring.
- SQLite-backed history with charts from one hour to seven days.
- Rule-based alerts and a green/yellow/red health banner that do not require AI.
- Optional local AI chat and plain-language explanations powered by Ollama.
- Runtime model, language, temperature, and context-length switching.
- English and Chinese UI with instant language switching.
- Spotlight file search and bounded large-file scanning.
- Plugin-style diagnostic toolbox with nine built-in probes.
- Bounded, header-only packet capture with protocol and top-session summaries.
- A bilingual `bamai` CLI for daemon, log, model, and autostart management.

## Requirements

- macOS on Apple Silicon.
- Python 3.12 or later.
- Optional: [Ollama](https://ollama.com/) for local AI features.

## Quickstart

```bash
git clone <repository-url> bamai
cd bamai
./bamai start
```

Open <http://127.0.0.1:8737>. The first start creates `.venv`, installs dependencies, and downloads the local chart asset. Run `./bamai status`, `./bamai logs`, or `./bamai stop` to manage the service.

AI is optional. To enable it:

```bash
brew install ollama
ollama serve
ollama pull qwen3:4b
./bamai restart --with-ai
```

The legacy `./run.sh` entry point remains available and starts Bamai in the foreground.

## Recommended models

| Model | Approximate download | Recommended system memory | Best for |
| --- | ---: | ---: | --- |
| `qwen3:4b` | 3 GB | 8 GB | Fast responses and the default experience |
| `qwen3:8b` | 6 GB | 16 GB | More detailed explanations |
| `qwen3:14b` | 10 GB | 24 GB | Higher-quality reasoning on capable Macs |

Install or switch models from Settings, with `./bamai model pull <name>`, or with `./bamai model use <name>`.

## Toolbox

The Toolbox provides read-only, structured diagnostics for:

- Ping, traceroute, DNS, port connectivity, and macOS `networkQuality`.
- Memory pressure and processes with sustained RSS growth.
- Wi-Fi signal/channel details and battery health.
- Bounded packet capture with protocol distribution and top five-tuple sessions.

Every probe validates its parameters and invokes system commands as argument lists without a shell. Jobs run in the background with a maximum concurrency of three.

### Packet-capture authorization

Packet capture is disabled until the current user can read `/dev/bpf0`. Enable it with:

```bash
sudo ./scripts/enable-capture.sh
```

The script follows Wireshark's `access_bpf` group approach. It does not run the Bamai server as root. Log out and back in after enabling access. To remove the LaunchDaemon, user/group changes created by Bamai, and BPF permissions:

```bash
sudo ./scripts/disable-capture.sh
```

Captures use a 96-byte snap length, disable name resolution, stop after at most 60 seconds or 2,000 packets, and are stored only in `~/.bamai/captures/`. They are never inserted into SQLite or sent to a cloud service.

## Architecture

```text
Browser UI (HTML/CSS/JavaScript)
          │ REST + WebSocket on 127.0.0.1:8737
          ▼
FastAPI server ───── SQLite (~/.bamai/data.db)
     │    │
     │    ├── Monitor + rule engine + background jobs
     │    └── Toolbox registry ── macOS built-in commands
     │
     └── Optional Ollama client ── local model on 127.0.0.1:11434
```

The collector samples system metrics, the rule engine creates structured events, FastAPI exposes REST/WebSocket endpoints, and the static dashboard renders local data. Ollama is isolated behind an optional client so monitoring remains available when AI is offline.

## Screenshots

Placeholders for the first public release:

- [Dashboard](docs/screenshots/dashboard.png)
- [Toolbox](docs/screenshots/toolbox.png)
- [Settings and local AI](docs/screenshots/settings-chat.png)

See [docs/screenshots/](docs/screenshots/) for contribution notes.

## Development and testing

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt pytest ruff
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
```

Run the development server in the foreground:

```bash
./bamai start --foreground
```

Ollama is not required for tests; AI responses are mocked. See [CONTRIBUTING.md](CONTRIBUTING.md) for probe development and code-style guidance.

## Data and configuration

- Database: `~/.bamai/data.db`
- Runtime settings: `~/.bamai/config.json`
- PID and logs: `~/.bamai/bamai.pid`, `~/.bamai/bamai.log`
- Packet captures: `~/.bamai/captures/`
- Overrides: `BAMAI_DATA_DIR`, `BAMAI_DB_PATH`, `BAMAI_CONFIG_PATH`, `BAMAI_CAPTURES_DIR`, and `BAMAI_OLLAMA_URL`

On first start, Bamai safely copies a legacy `~/.macpilot` directory to `~/.bamai` while retaining the original as a backup.

## License

[MIT](LICENSE) © Bamai contributors.
