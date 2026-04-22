#!/usr/bin/env bash
# Telemetry sidecar — runs alongside a k6 scenario and captures
# host-side resource data at 5s cadence. The k6 run itself produces
# API-side latency + error metrics; this script covers the pieces k6
# can't see (container RSS, GPU utilization, Qdrant index growth).
#
# Usage:
#
#   scripts/perf/telemetry.sh start <run-label>
#   # ... k6 run in parallel ...
#   scripts/perf/telemetry.sh stop
#   scripts/perf/telemetry.sh summarize <run-label>
#
# Or, for convenience, `make perf-load` wraps this with the k6 run.
#
# Outputs:
#
#   ~/perf-runs/<label>/
#     docker-stats.jsonl     — docker stats sample every 5s
#     nvidia-smi.jsonl       — GPU sample every 5s (if present)
#     musubi-access.log      — tail of musubi-core access logs
#     summary.md             — post-run rollup (run `summarize`)

set -euo pipefail

# -------- config --------------------------------------------------

RUN_ROOT="${PERF_RUN_ROOT:-$HOME/perf-runs}"
SAMPLE_INTERVAL_S="${SAMPLE_INTERVAL_S:-5}"
PID_FILE="/tmp/musubi-perf-telemetry.pid"

# -------- helpers -------------------------------------------------

log() { printf '[telemetry %s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

die() { log "ERROR: $*"; exit 1; }

usage() {
  sed -n '2,20p' "$0"
  exit 1
}

# -------- commands ------------------------------------------------

cmd_start() {
  local label="${1:?missing run label}"
  local dir="$RUN_ROOT/$label"
  mkdir -p "$dir"

  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    die "telemetry already running as pid $(cat "$PID_FILE"). 'stop' it first."
  fi

  log "starting telemetry → $dir (sample every ${SAMPLE_INTERVAL_S}s)"

  # Launch the sampler subshell. It traps SIGTERM so `stop` can
  # flush cleanly. `exec` replaces shell, keeping PID stable for
  # the stop command.
  (
    trap 'log "telemetry sampler stopping"; exit 0' TERM INT
    while :; do
      ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

      # Docker stats — JSON per container, one line per container per sample.
      if command -v docker >/dev/null 2>&1; then
        docker stats --no-stream --format '{{json .}}' 2>/dev/null \
          | jq -c --arg ts "$ts" '. + {ts: $ts}' \
          >> "$dir/docker-stats.jsonl" || true
      fi

      # GPU — skip silently if nvidia-smi isn't installed (dev machines).
      if command -v nvidia-smi >/dev/null 2>&1; then
        nvidia-smi --query-gpu=timestamp,name,utilization.gpu,utilization.memory,memory.used,memory.free,temperature.gpu \
                   --format=csv,noheader,nounits 2>/dev/null \
          | awk -v ts="$ts" 'BEGIN{FS=", "}{
              printf "{\"ts\":\"%s\",\"name\":\"%s\",\"gpu_util\":%s,\"mem_util\":%s,\"mem_used_mib\":%s,\"mem_free_mib\":%s,\"temp_c\":%s}\n",
                ts, $2, $3, $4, $5, $6, $7
            }' \
          >> "$dir/nvidia-smi.jsonl" || true
      fi

      sleep "$SAMPLE_INTERVAL_S"
    done
  ) &

  echo $! > "$PID_FILE"
  log "started as pid $(cat "$PID_FILE")"
}

cmd_stop() {
  if [[ ! -f "$PID_FILE" ]]; then
    log "not running (no pidfile)"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid"
    wait "$pid" 2>/dev/null || true
    log "stopped pid $pid"
  else
    log "pid $pid not running"
  fi
  rm -f "$PID_FILE"
}

cmd_summarize() {
  local label="${1:?missing run label}"
  local dir="$RUN_ROOT/$label"
  [[ -d "$dir" ]] || die "no run dir: $dir"

  local summary="$dir/summary.md"

  {
    printf '# perf run: %s\n\n' "$label"
    printf 'generated: %s\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    printf '## Docker stats rollup\n\n'
    if [[ -s "$dir/docker-stats.jsonl" ]]; then
      jq -rn --slurpfile d "$dir/docker-stats.jsonl" '
        [$d[] | {
          name: .Name,
          cpu: (.CPUPerc | rtrimstr("%") | tonumber? // 0),
          mem_pct: (.MemPerc | rtrimstr("%") | tonumber? // 0)
        }]
        | group_by(.name)
        | map({
            name: .[0].name,
            samples: length,
            cpu_p50: (map(.cpu) | sort | .[(length/2 | floor)]),
            cpu_p95: (map(.cpu) | sort | .[(length*0.95 | floor)]),
            cpu_max: (map(.cpu) | max),
            mem_p50: (map(.mem_pct) | sort | .[(length/2 | floor)]),
            mem_p95: (map(.mem_pct) | sort | .[(length*0.95 | floor)]),
            mem_max: (map(.mem_pct) | max)
          })
        | .[]
        | "- **\(.name)** (samples=\(.samples)): cpu p50=\(.cpu_p50)% p95=\(.cpu_p95)% max=\(.cpu_max)% | mem p50=\(.mem_p50)% p95=\(.mem_p95)% max=\(.mem_max)%"
      '
    else
      printf '_no docker-stats.jsonl captured_\n'
    fi
    printf '\n'

    printf '## GPU rollup\n\n'
    if [[ -s "$dir/nvidia-smi.jsonl" ]]; then
      jq -rn --slurpfile g "$dir/nvidia-smi.jsonl" '
        [$g[]]
        | {
            samples: length,
            gpu_util_p50: (map(.gpu_util) | sort | .[(length/2 | floor)]),
            gpu_util_p95: (map(.gpu_util) | sort | .[(length*0.95 | floor)]),
            gpu_util_max: (map(.gpu_util) | max),
            mem_used_p50: (map(.mem_used_mib) | sort | .[(length/2 | floor)]),
            mem_used_p95: (map(.mem_used_mib) | sort | .[(length*0.95 | floor)]),
            mem_used_max: (map(.mem_used_mib) | max),
            mem_free_min: (map(.mem_free_mib) | min),
            temp_max: (map(.temp_c) | max)
          }
        | "- samples=\(.samples)",
          "- gpu_util: p50=\(.gpu_util_p50)% p95=\(.gpu_util_p95)% max=\(.gpu_util_max)%",
          "- mem_used: p50=\(.mem_used_p50)MiB p95=\(.mem_used_p95)MiB max=\(.mem_used_max)MiB",
          "- mem_free_min: \(.mem_free_min)MiB  ← must stay ≥ 1024 per perf gate",
          "- temp_max: \(.temp_max)°C"
      '
    else
      printf '_no nvidia-smi.jsonl — telemetry ran on a host without NVIDIA_\n'
    fi
    printf '\n'

    printf '## Files\n\n'
    ls -lh "$dir" | awk 'NR>1 {printf "- `%s` (%s)\n", $NF, $5}'
  } > "$summary"

  log "summary written: $summary"
  cat "$summary"
}

# -------- dispatch ------------------------------------------------

case "${1:-}" in
  start) shift; cmd_start "$@" ;;
  stop) cmd_stop ;;
  summarize) shift; cmd_summarize "$@" ;;
  *) usage ;;
esac
