#!/usr/bin/env bash
# dev-startup.sh — install kent locally, launch the 3D palace viewer, drop
# into the interactive REPL.
#
# Steps:
#   1. ascii banner + boot sequence
#   2. uv sync           install all project + dev dependencies into .venv
#   3. credentials.json  copy any non-placeholder keys to ~/.kent
#   4. kent viz          background — 3D palace viewer at :8765 (auto-opens
#                        in default browser)
#   5. kent              foreground — interactive REPL; viz keeps running
#                        until the REPL exits, then cleanup trap kills it.
#
# Env overrides:
#   KENT_VIZ_PORT   port for the viz server         (default: 8765)
#   KENT_NO_VIZ=1   skip viz (REPL-only)
#   KENT_NO_OPEN=1  skip auto-opening the browser
#   KENT_HOME       kent home dir                   (default: ~/.kent)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CREDS_REPO="$SCRIPT_DIR/credentials.json"
KENT_HOME="${KENT_HOME:-$HOME/.kent}"
CREDS_DEST="$KENT_HOME/credentials.json"
VIZ_PORT="${KENT_VIZ_PORT:-8765}"
VIZ_LOG="$KENT_HOME/viz.log"
VIZ_PID=""
GATEWAY_LOG="$KENT_HOME/gateway.log"
GATEWAY_PID=""

# ---------- colors / fx ---------------------------------------------------- #

if [[ -t 1 ]]; then
    C_RESET=$'\033[0m'
    C_DIM=$'\033[2m'
    C_BOLD=$'\033[1m'
    C_CYAN=$'\033[38;5;51m'
    C_PURPLE=$'\033[38;5;141m'
    C_GOLD=$'\033[38;5;220m'
    C_GREEN=$'\033[38;5;120m'
    C_RED=$'\033[38;5;203m'
    C_GREY=$'\033[38;5;244m'
    C_INK=$'\033[38;5;39m'
else
    C_RESET="" C_DIM="" C_BOLD="" C_CYAN="" C_PURPLE="" C_GOLD=""
    C_GREEN="" C_RED="" C_GREY="" C_INK=""
fi

typeout() {
    # Print a string character-by-character. Args: text, delay-per-char (sec).
    local s="$1" delay="${2:-0.010}"
    local i
    for (( i=0; i<${#s}; i++ )); do
        printf "%s" "${s:$i:1}"
        sleep "$delay" 2>/dev/null || true
    done
    printf "\n"
}

spin_while() {
    # Show a spinner while $1 (PID) is alive. Replace with a check on exit.
    local pid="$1" msg="$2"
    local frames=( '⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏' )
    local i=0
    tput civis 2>/dev/null || true
    while kill -0 "$pid" 2>/dev/null; do
        printf "\r  ${C_CYAN}%s${C_RESET} %s" "${frames[i]}" "$msg"
        i=$(( (i + 1) % ${#frames[@]} ))
        sleep 0.08 2>/dev/null || true
    done
    tput cnorm 2>/dev/null || true
    if wait "$pid"; then
        printf "\r  ${C_GREEN}✓${C_RESET} %s\n" "$msg"
        return 0
    else
        printf "\r  ${C_RED}✗${C_RESET} %s\n" "$msg"
        return 1
    fi
}

boot_line() {
    # Args: label, message, color (default: cyan)
    local label="$1" msg="$2" color="${3:-$C_CYAN}"
    printf "  ${C_GREY}[${C_RESET}${color}%-5s${C_RESET}${C_GREY}]${C_RESET} %s\n" "$label" "$msg"
}

cleanup() {
    if [[ -n "$VIZ_PID" ]] && kill -0 "$VIZ_PID" 2>/dev/null; then
        printf "\n  ${C_GREY}[viz  ]${C_RESET} stopping (pid %s)…" "$VIZ_PID"
        kill "$VIZ_PID" 2>/dev/null || true
        wait "$VIZ_PID" 2>/dev/null || true
        printf " ${C_GREEN}done${C_RESET}\n"
    fi
    if [[ -n "$GATEWAY_PID" ]] && kill -0 "$GATEWAY_PID" 2>/dev/null; then
        printf "\n  ${C_GREY}[gw   ]${C_RESET} stopping (pid %s)…" "$GATEWAY_PID"
        kill "$GATEWAY_PID" 2>/dev/null || true
        wait "$GATEWAY_PID" 2>/dev/null || true
        printf " ${C_GREEN}done${C_RESET}\n"
    fi
    tput cnorm 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ---------- banner --------------------------------------------------------- #

print_banner() {
    printf "\n"
    # Render line-by-line so it has a brief reveal animation on tty.
    local lines=(
"     ██╗  ██╗███████╗███╗   ██╗████████╗"
"     ██║ ██╔╝██╔════╝████╗  ██║╚══██╔══╝"
"     █████╔╝ █████╗  ██╔██╗ ██║   ██║   "
"     ██╔═██╗ ██╔══╝  ██║╚██╗██║   ██║   "
"     ██║  ██╗███████╗██║ ╚████║   ██║   "
"     ╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝   ╚═╝   "
    )
    local line
    for line in "${lines[@]}"; do
        printf "${C_PURPLE}%s${C_RESET}\n" "$line"
        sleep 0.04 2>/dev/null || true
    done
    printf "\n"
    typeout "     ${C_GOLD}◇${C_RESET}${C_DIM} a memory palace, drawn live ${C_RESET}${C_GOLD}◇${C_RESET}" 0.012
    printf "\n"
}

print_palace_glyph() {
    # Tiny ascii palace shown beneath the URL. Cute, not mission-critical.
    printf "${C_DIM}${C_INK}"
    cat <<'EOF'
                 ╱╲
                ╱  ╲
               ╱ ◈  ╲
              ╱──────╲
              │ ┌──┐ │
              │ │  │ │       wings · rooms · drawers
              └─┴──┴─┘       linked in real time
EOF
    printf "${C_RESET}\n"
}

# ---------- 0. banner ------------------------------------------------------ #

print_banner

# ---------- 1. install ----------------------------------------------------- #

if ! command -v uv >/dev/null 2>&1; then
    boot_line "boot " "${C_RED}uv not installed${C_RESET} — get it from https://docs.astral.sh/uv/" "$C_RED"
    exit 1
fi

boot_line "boot " "syncing python deps"
( uv sync >/dev/null 2>&1 ) &
spin_while $! "uv sync" || {
    boot_line "boot " "${C_RED}uv sync failed${C_RESET} — re-run with: ${C_BOLD}uv sync${C_RESET}" "$C_RED"
    exit 1
}

# ---------- 2. credentials check ------------------------------------------ #

if [ ! -f "$CREDS_REPO" ]; then
    echo
    boot_line "creds" "${C_DIM}no credentials.json at repo root${C_RESET}" "$C_GOLD"
    printf "          copy ${C_BOLD}credentials.json.example${C_RESET} → ${C_BOLD}credentials.json${C_RESET} and fill in keys,\n"
    printf "          or run: ${C_BOLD}uv run kent auth${C_RESET}\n"
    exit 0
fi

VALID_KEYS_JSON="$(uv run --quiet python - "$CREDS_REPO" <<'PY'
import json, sys
path = sys.argv[1]
try:
    data = json.loads(open(path).read())
except Exception as e:
    print(f"__ERROR__:{e}", file=sys.stderr)
    sys.exit(2)
if not isinstance(data, dict):
    print("__ERROR__:credentials.json must be a JSON object", file=sys.stderr)
    sys.exit(2)
valid = {
    k: v for k, v in data.items()
    if isinstance(v, str) and v.strip() and "<" not in v and not v.startswith("apikey-<")
}
print(json.dumps(valid))
PY
)"

if [ -z "$VALID_KEYS_JSON" ] || [ "$VALID_KEYS_JSON" = "{}" ]; then
    boot_line "creds" "${C_DIM}no valid keys (only placeholders)${C_RESET}" "$C_GOLD"
    printf "          edit ${C_BOLD}%s${C_RESET} and replace placeholder values\n" "$CREDS_REPO"
    exit 0
fi

# ---------- 3. sync to ~/.kent -------------------------------------------- #

mkdir -p "$KENT_HOME"
KEY_COUNT="$(uv run --quiet python - "$CREDS_DEST" "$VALID_KEYS_JSON" <<'PY'
import json, os, sys
dest, valid_json = sys.argv[1], sys.argv[2]
new = json.loads(valid_json)
existing = {}
if os.path.exists(dest):
    try:
        existing = json.loads(open(dest).read())
        if not isinstance(existing, dict):
            existing = {}
    except Exception:
        existing = {}
existing.update(new)
with open(dest, "w") as f:
    json.dump(existing, f, indent=2)
os.chmod(dest, 0o600)
print(len(new))
PY
)"
boot_line "creds" "synced ${C_BOLD}${KEY_COUNT}${C_RESET} key(s) → ~/.kent/credentials.json" "$C_GREEN"

# ---------- 4. launch viz (background) ----------------------------------- #

if [ "${KENT_NO_VIZ:-0}" = "1" ]; then
    boot_line "viz  " "${C_DIM}skipped (KENT_NO_VIZ=1)${C_RESET}" "$C_GOLD"
else
    boot_line "viz  " "spawning 3D palace viewer on :${VIZ_PORT}"
    : > "$VIZ_LOG"
    ( uv run --quiet kent viz --port "$VIZ_PORT" >> "$VIZ_LOG" 2>&1 ) &
    VIZ_PID=$!

    # Poll until the server binds (or give up after ~12s). Probe via bash's
    # built-in /dev/tcp so this works on minimal images without curl/wget.
    BOOT_OK=""
    for _ in $(seq 1 60); do
        if ! kill -0 "$VIZ_PID" 2>/dev/null; then
            break
        fi
        if (exec 3<>"/dev/tcp/127.0.0.1/$VIZ_PORT") 2>/dev/null; then
            exec 3<&- 3>&-
            BOOT_OK=1
            break
        fi
        sleep 0.2
    done

    if [ -z "$BOOT_OK" ] || ! kill -0 "$VIZ_PID" 2>/dev/null; then
        boot_line "viz  " "${C_RED}failed to start${C_RESET} — see ${C_BOLD}${VIZ_LOG}${C_RESET}" "$C_RED"
        VIZ_PID=""
    else
        boot_line "viz  " "live at ${C_BOLD}${C_INK}http://127.0.0.1:${VIZ_PORT}${C_RESET}" "$C_GREEN"
        if [ "${KENT_NO_OPEN:-0}" != "1" ]; then
            if command -v open >/dev/null 2>&1; then
                open "http://127.0.0.1:$VIZ_PORT" 2>/dev/null || true
            elif command -v xdg-open >/dev/null 2>&1; then
                xdg-open "http://127.0.0.1:$VIZ_PORT" >/dev/null 2>&1 || true
            fi
        fi
    fi
fi

# ---------- 4b. discover token + heartbeat config (first-run prompt) ----- #

HEARTBEAT_MD="$KENT_HOME/HEARTBEAT.md"
HAS_TOKEN="0"

if [ "${KENT_NO_GATEWAY:-0}" != "1" ]; then
    HAS_TOKEN="$(uv run --quiet python - "$CREDS_DEST" <<'PY'
import json, os, sys
dest = sys.argv[1]
if not os.path.exists(dest):
    print("0"); sys.exit(0)
try:
    data = json.loads(open(dest).read())
    print("1" if isinstance(data, dict) and data.get("discord_bot_token") else "0")
except Exception:
    print("0")
PY
)"
fi

if [ "${KENT_NO_HEARTBEAT:-0}" != "1" ] && [ "$HAS_TOKEN" = "1" ]; then
    HB_ALREADY_SET="$(uv run --quiet python - "$KENT_HOME/config.json" <<'PY'
import json, sys
p = sys.argv[1]
try:
    cfg = json.loads(open(p).read())
    val = (cfg.get("gateway") or {}).get("heartbeat_interval")
    print("1" if val is not None else "0")
except Exception:
    print("0")
PY
)"
    if [ "$HB_ALREADY_SET" = "0" ]; then
        printf "  How often should the heartbeat tick? (30s/5m/30m/1h/off, default 30m): "
        read -r HB_INTERVAL || HB_INTERVAL=""
        HB_INTERVAL="${HB_INTERVAL:-30m}"

        HB_CHANNEL_ID=""
        if [ "$HB_INTERVAL" != "off" ]; then
            printf "  Heartbeat Discord channel id (numeric, blank = skip): "
            read -r HB_CHANNEL_ID || HB_CHANNEL_ID=""
        fi

        uv run --quiet python - "$KENT_HOME/config.json" "$HB_INTERVAL" "$HB_CHANNEL_ID" <<'PY'
import json, sys, os
p, interval, channel = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    cfg = json.loads(open(p).read()) if os.path.exists(p) else {}
except Exception:
    cfg = {}
block = cfg.get("gateway") or {}
block["heartbeat_interval"] = interval
if channel:
    try:
        block["heartbeat_channel_id"] = int(channel)
    except ValueError:
        pass
cfg["gateway"] = block
open(p, "w").write(json.dumps(cfg, indent=2))
PY
        boot_line "hb   " "heartbeat configured: interval=${HB_INTERVAL} channel=${HB_CHANNEL_ID:-<none>}" "$C_GREEN"
    fi

    if [ ! -f "$HEARTBEAT_MD" ]; then
        uv run --quiet python -c "
from agent.gateway.heartbeat import default_heartbeat_md_text
import pathlib
p = pathlib.Path('$HEARTBEAT_MD')
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(default_heartbeat_md_text())
"
        boot_line "hb   " "seeded ${C_BOLD}${HEARTBEAT_MD}${C_RESET}" "$C_GREEN"
    fi

    HB_CURRENT="$(uv run --quiet python - "$KENT_HOME/config.json" <<'PY'
import json, sys
p = sys.argv[1]
try:
    block = (json.loads(open(p).read()).get("gateway") or {})
    interval = block.get("heartbeat_interval") or "<unset>"
    channel = block.get("heartbeat_channel_id") or "<unset>"
    print(f"{interval}|{channel}")
except Exception:
    print("<unset>|<unset>")
PY
)"
    HB_INTERVAL_NOW="${HB_CURRENT%%|*}"
    HB_CHANNEL_NOW="${HB_CURRENT##*|}"
    boot_line "hb   " "tick=${C_BOLD}${HB_INTERVAL_NOW}${C_RESET} channel=${C_BOLD}${HB_CHANNEL_NOW}${C_RESET} file=${C_BOLD}${HEARTBEAT_MD}${C_RESET}" "$C_GREEN"
fi

# ---------- 4c. discord e2e smoke test ------------------------------------ #
# Validates: token works, intents wired, channel reachable, send permission.
# Runs *before* the long-lived gateway spawn so token/permission errors
# surface immediately instead of hiding in gateway.log.

GW_TEST_OK="0"
if [ "${KENT_NO_GATEWAY_TEST:-0}" = "1" ] || [ "$HAS_TOKEN" != "1" ]; then
    : # skip
else
    boot_line "gwtst" "running Discord connectivity smoke test"
    if uv run --quiet kent gateway test --send >/tmp/kent-gwtest.$$ 2>&1; then
        GW_TEST_OK="1"
        # Show key lines from the test output (✓ marks)
        while IFS= read -r line; do
            [ -z "$line" ] && continue
            printf "       ${C_DIM}%s${C_RESET}\n" "$line"
        done < /tmp/kent-gwtest.$$
        boot_line "gwtst" "${C_BOLD}all checks passed${C_RESET}" "$C_GREEN"
    else
        boot_line "gwtst" "${C_RED}failed${C_RESET} — see output below" "$C_RED"
        while IFS= read -r line; do
            [ -z "$line" ] && continue
            printf "       ${C_DIM}%s${C_RESET}\n" "$line"
        done < /tmp/kent-gwtest.$$
        printf "       ${C_GOLD}fix the issue (token / channel / perms) and re-run dev-startup.sh${C_RESET}\n"
    fi
    rm -f /tmp/kent-gwtest.$$
fi

# ---------- 4d. launch gateway (background, optional) -------------------- #

if [ "${KENT_NO_GATEWAY:-0}" = "1" ]; then
    boot_line "gw   " "${C_DIM}skipped (KENT_NO_GATEWAY=1)${C_RESET}" "$C_GOLD"
elif [ "$HAS_TOKEN" != "1" ]; then
    boot_line "gw   " "${C_DIM}disabled (no token — run \`kent gateway config\`)${C_RESET}" "$C_GOLD"
elif [ "$GW_TEST_OK" != "1" ] && [ "${KENT_NO_GATEWAY_TEST:-0}" != "1" ]; then
    boot_line "gw   " "${C_GOLD}not spawned (smoke test failed; set KENT_NO_GATEWAY_TEST=1 to bypass)${C_RESET}" "$C_GOLD"
else
    boot_line "gw   " "spawning Discord gateway"
    : > "$GATEWAY_LOG"
    rm -f "$KENT_HOME/gateway.status.json" 2>/dev/null || true
    ( uv run --quiet kent gateway run >> "$GATEWAY_LOG" 2>&1 ) &
    GATEWAY_PID=$!

    # Poll for on_ready (status file written) — give it 20s to reach Discord.
    GW_OK=""
    for _ in $(seq 1 100); do
        if ! kill -0 "$GATEWAY_PID" 2>/dev/null; then
            break
        fi
        if [ -f "$KENT_HOME/gateway.status.json" ]; then
            GW_OK=1
            break
        fi
        sleep 0.2
    done

    if [ -z "$GW_OK" ] || ! kill -0 "$GATEWAY_PID" 2>/dev/null; then
        boot_line "gw   " "${C_RED}failed to come online${C_RESET} — see ${C_BOLD}${GATEWAY_LOG}${C_RESET}" "$C_RED"
        if kill -0 "$GATEWAY_PID" 2>/dev/null; then
            kill "$GATEWAY_PID" 2>/dev/null || true
        fi
        GATEWAY_PID=""
    else
        GW_USER="$(uv run --quiet python - "$KENT_HOME/gateway.status.json" <<'PY'
import json, sys
try:
    print(json.loads(open(sys.argv[1]).read()).get("user") or "")
except Exception:
    print("")
PY
)"
        if [ -n "$GW_USER" ]; then
            boot_line "gw   " "online as ${C_BOLD}${GW_USER}${C_RESET} (pid ${C_BOLD}${GATEWAY_PID}${C_RESET}) — log: ${C_BOLD}${GATEWAY_LOG}${C_RESET}" "$C_GREEN"
        else
            boot_line "gw   " "online (pid ${C_BOLD}${GATEWAY_PID}${C_RESET}) — log: ${C_BOLD}${GATEWAY_LOG}${C_RESET}" "$C_GREEN"
        fi
    fi
fi

echo
print_palace_glyph

# ---------- 5. drop into REPL --------------------------------------------- #

printf "${C_DIM}${C_GREY}  ─────────────────────────────────────────────────────────────${C_RESET}\n"
typeout "  ${C_BOLD}entering REPL${C_RESET}${C_DIM} — type /help for commands, /exit to quit${C_RESET}" 0.008
printf "${C_DIM}${C_GREY}  ─────────────────────────────────────────────────────────────${C_RESET}\n"
echo

# Run in foreground (NOT exec) so the EXIT trap fires and viz cleanup runs
# when the REPL exits. `set -e` is suppressed for this call so Ctrl-C in
# the REPL doesn't skip cleanup.
set +e
uv run kent
REPL_RC=$?
set -e

exit "$REPL_RC"
