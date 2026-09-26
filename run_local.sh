#!/bin/bash
# Local preview: regenerate the page every INTERVAL seconds in a background loop and serve
# the output directory with Python's built-in HTTP server. Ctrl-C (or SIGTERM/SIGHUP) stops
# both, including a generator run that is in progress.
# Usage:  ./run_local.sh [port] [out_dir]        (defaults: 8080, ./out)
# Env:    INTERVAL=120        seconds between generator runs
#         BIND=0.0.0.0        address the preview server listens on (0.0.0.0 = every
#                             interface, so the page can be opened from other machines;
#                             BIND=127.0.0.1 keeps it on this machine)
#         WEATHER_* variables are passed through to the generator.
# Exit status: the server's own status when it stops by itself (e.g. 1 when the port is
# already in use), 128+signal when the script is interrupted (130 for Ctrl-C).
cd "$(dirname "$0")" || exit 1

PORT="${1:-8080}"
OUT="${2:-./out}"
INTERVAL="${INTERVAL:-120}"
BIND="${BIND:-0.0.0.0}"
export WEATHER_OUT_DIR="$OUT"
export WEATHER_CACHE_DIR="${WEATHER_CACHE_DIR:-./.cache-dev}"
DEV_USER_AGENT="local-weather-awareness-dev (+https://github.com/kirxkirx/local-weather-awareness)"
export WEATHER_USER_AGENT="${WEATHER_USER_AGENT:-$DEV_USER_AGENT}"

mkdir -p "$OUT" || exit 1

loop() {
    while true; do
        start=$(date +%s)
        python3 ./make_weather_page.py
        elapsed=$(( $(date +%s) - start ))
        sleep_time=$(( INTERVAL - elapsed ))
        [ "$sleep_time" -gt 0 ] && sleep "$sleep_time"
    done
}

LOOP_PID=""
SERVER_PID=""

# Stop both background jobs. Each runs in its own process group (set -m below), so killing
# the group also stops what the job started: the generator run and the loop's sleep, not
# just the loop's subshell (which would leave a running generator orphaned).
stop_jobs() {
    trap - INT TERM HUP EXIT
    local pid
    for pid in $SERVER_PID $LOOP_PID; do
        kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
    done
    for pid in $SERVER_PID $LOOP_PID; do
        wait "$pid" 2>/dev/null
    done
}
trap 'stop_jobs; exit 130' INT
trap 'stop_jobs; exit 143' TERM
trap 'stop_jobs; exit 129' HUP
trap 'stop_jobs' EXIT

# Job control: every background job gets its own process group (see stop_jobs). It also
# keeps Ctrl-C away from the jobs themselves, so only this script reacts to it, via the trap.
set -m

loop &
LOOP_PID=$!

case "$BIND" in
    0.0.0.0|::) where="http://$(hostname):$PORT/  (also http://localhost:$PORT/)" ;;
    *:*)        where="http://[$BIND]:$PORT/" ;;
    *)          where="http://$BIND:$PORT/" ;;
esac
echo "local-weather-awareness local preview: $where"
echo "regenerating every ${INTERVAL}s into $OUT; log lines from the generator follow"

python3 -m http.server "$PORT" --bind "$BIND" --directory "$OUT" &
SERVER_PID=$!

# `wait` returns as soon as the server exits, or at once when a trapped signal arrives
# (the trap then stops both jobs and exits 128+signal).
wait "$SERVER_PID"
rc=$?
if [ "$rc" -ne 0 ]; then
    echo "run_local.sh: preview server exited with status $rc; stopping the generator loop" >&2
fi
stop_jobs
exit "$rc"
