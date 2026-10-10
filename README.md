# strata web monitor

Live web dashboard for the **strata** LLM engine daemon: current stage
(thinking / answering / tool call / prefill), token/s output, CPU cores used,
context-window usage, GPU stats, system health, and time-series plots —
all rendered on a single page with hand-rolled canvas charts (no external assets).

- Pure Python 3 stdlib + `psutil`, no web framework, no CDN.
- Reads `strata.service` journal logs (`journalctl -f`) plus live CPU/RAM/GPU
  sampling (`psutil`, `nvidia-smi`). Historical stage/tok/context rows are
  backfilled at their real timestamps from the log; CPU/GPU/RAM fill in live.
- Data stored in SQLite (WAL); 14-day retention by default; history is
  bucketed server-side.

## Install (Ubuntu 24.04, root)

On the machine running the strata engine (needs local journal + GPU access):

```sh
sudo ./install.sh          # installs python3-psutil if missing, installs the
                           # systemd unit and starts it
```

Dashboard: `http://<host>:8090/`
Config: `/etc/strata-monitor/config.json` (restart the unit after editing)
Logs: `journalctl -u strata-monitor -f`

## Config

| key                | default                     | meaning                                              |
|--------------------|-----------------------------|------------------------------------------------------|
| `unit`             | `strata.service`            | systemd unit followed for engine lifecycle           |
| `bind` / `port`    | `0.0.0.0` / `8090`          | HTTP listen address                                  |
| `log_file`         | `null`                      | null = live journal; a path = replay that file       |
| `file_position`    | `end`                       | replay start: `end` or `start`                       |
| `preload_hours`    | `24`                        | backfill this much history on startup                |
| `sample_interval_s`| `1.0`                       | sampling while engine is active                      |
| `idle_interval_s`  | `10.0`                      | sampling while engine is idle                        |
| `gpu_interval_s`   | `3.0`                       | `nvidia-smi` polling interval                        |
| `retention_days`   | `14`                        | prune samples older than this                        |
| `stale_after_s`    | `30`                        | engine marked stale if no log line for this long     |
| `db`               | `/var/lib/strata-monitor/monitor.db` | SQLite database                           |

## Local testing (replay mode)

Without journal access you can replay a log file:

```sh
python3 src/monitor.py \
  --log-file /path/to/strata.log \
  --file-position start --port 8090 --bind 127.0.0.1
```

## Layout

```
src/
  monitor.py     entry point, config merge, shutdown
  parser.py      journal line -> typed events
  collector.py   journal follower + CPU/GPU/RAM sampler + state machine
  store.py       SQLite time-series store, bucketed history, pruning
  server.py      stdlib HTTP API + static files
  static/        dashboard (index.html, style.css, app.js)
config.json          production defaults (installed to /etc/strata-monitor/)
strata-monitor.service  systemd unit (hardened, DynamicUser)
install.sh        one-shot installer
```

### Stages

`idle`, `starting`, `prefill`, `thinking`, `answering`, `tool_call`,
`stopped`, `error` — logged in `/api/state` and used as the plot background bands.

## API

- `GET /api/state` — live snapshot (stage, tok/s, context, CPU, GPU, last request)
- `GET /api/history?window=3600` — bucketed time series (window in seconds)
- `GET /api/health` — process liveness