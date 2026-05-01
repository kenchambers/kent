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
#   KENT_VIZ_HOST   bind address for viz            (default: 0.0.0.0 on WSL, 127.0.0.1 elsewhere)
#   KENT_NO_VIZ=1   skip viz (REPL-only)
#   KENT_NO_OPEN=1  skip auto-opening the browser
#   KENT_NO_LAN=1   skip Windows portproxy/firewall setup (WSL only)
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

is_wsl() {
    grep -qi microsoft /proc/version 2>/dev/null
}

detect_system() {
    # Returns one of: wsl | linux | macos | windows_bash | unknown
    local uname_s
    uname_s="$(uname -s 2>/dev/null || echo "unknown")"
    case "$uname_s" in
        Linux)
            if is_wsl; then echo "wsl"; else echo "linux"; fi ;;
        Darwin)
            echo "macos" ;;
        MINGW*|CYGWIN*|MSYS*)
            echo "windows_bash" ;;
        *)
            echo "unknown" ;;
    esac
}

setup_wsl_lan_forward() {
    # Configure Windows port-forward + firewall so the viz port is reachable
    # from other LAN devices. Idempotent: only triggers UAC when the existing
    # rule is missing or its connectaddress no longer matches the WSL IP
    # (which changes across WSL restarts in default networking mode).
    #
    # Args: wsl_ip, port
    local wsl_ip="$1" port="$2"
    local rule_name="kent viz $port"
    local ps_dir="/mnt/c/Windows/Temp"
    local ps_path="$ps_dir/kent-lan-setup-$port.ps1"
    local ps_path_win="C:\\Windows\\Temp\\kent-lan-setup-$port.ps1"

    if ! command -v powershell.exe >/dev/null 2>&1; then
        boot_line "lan  " "${C_GOLD}powershell.exe not found — skipping Windows setup${C_RESET}" "$C_GOLD"
        return 1
    fi

    # Pre-check: skip elevation if rule is already correct.
    local current_target
    current_target="$(powershell.exe -NoProfile -Command "(netsh interface portproxy show v4tov4 | Select-String -Pattern '0\.0\.0\.0\s+$port\s+(\S+)\s+$port' | ForEach-Object { \$_.Matches.Groups[1].Value }) -join ''" 2>/dev/null | tr -d '\r' | head -1)"
    local fw_exists
    fw_exists="$(powershell.exe -NoProfile -Command "if (Get-NetFirewallRule -DisplayName '$rule_name' -ErrorAction SilentlyContinue) { 'True' } else { 'False' }" 2>/dev/null | tr -d '\r' | head -1)"

    if [ "$current_target" = "$wsl_ip" ] && [ "$fw_exists" = "True" ]; then
        boot_line "lan  " "Windows forward already configured (→ ${C_BOLD}${wsl_ip}:${port}${C_RESET})" "$C_GREEN"
        return 0
    fi

    # Write a small .ps1 to the Windows temp dir, then invoke it elevated.
    # Bash $vars expand here; PS $vars are escaped with \$.
    cat > "$ps_path" <<EOF
\$ErrorActionPreference = 'SilentlyContinue'
\$wslIp = "$wsl_ip"
\$port = $port
\$ruleName = "$rule_name"
# IP Helper service (iphlpsvc) is what actually binds the portproxy listener.
# If it's stopped, netsh rules are inert.
Set-Service -Name iphlpsvc -StartupType Automatic
Start-Service -Name iphlpsvc
& netsh interface portproxy delete v4tov4 listenport=\$port listenaddress=0.0.0.0 | Out-Null
& netsh interface portproxy add    v4tov4 listenport=\$port listenaddress=0.0.0.0 connectport=\$port connectaddress=\$wslIp | Out-Null
if (-not (Get-NetFirewallRule -DisplayName \$ruleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName \$ruleName -Direction Inbound -LocalPort \$port -Protocol TCP -Action Allow -Profile Any | Out-Null
}
EOF

    boot_line "lan  " "configuring Windows port-forward (UAC prompt may appear)…"
    if powershell.exe -NoProfile -Command "Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','$ps_path_win' -Verb RunAs -Wait" >/dev/null 2>&1; then
        # Re-verify
        current_target="$(powershell.exe -NoProfile -Command "(netsh interface portproxy show v4tov4 | Select-String -Pattern '0\.0\.0\.0\s+$port\s+(\S+)\s+$port' | ForEach-Object { \$_.Matches.Groups[1].Value }) -join ''" 2>/dev/null | tr -d '\r' | head -1)"
        if [ "$current_target" = "$wsl_ip" ]; then
            boot_line "lan  " "Windows forward live: 0.0.0.0:${port} → ${C_BOLD}${wsl_ip}:${port}${C_RESET}" "$C_GREEN"
            return 0
        fi
        boot_line "lan  " "${C_GOLD}forward not verified — UAC may have been declined${C_RESET}" "$C_GOLD"
        return 1
    else
        boot_line "lan  " "${C_RED}elevation failed${C_RESET} — run manually: ${C_BOLD}powershell -File ${ps_path_win}${C_RESET} (as admin)" "$C_RED"
        return 1
    fi
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
    typeout "     ${C_GOLD}◇${C_RESET}${C_DIM} a self improving persistent memory palace${C_RESET}${C_GOLD}◇${C_RESET}" 0.012
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

# ---------- 0b. system detection ------------------------------------------ #

SYS_TYPE="$(detect_system)"

case "$SYS_TYPE" in
    wsl)
        boot_line "sys  " "WSL2 on Windows  ${C_DIM}(full support)${C_RESET}" "$C_GREEN"
        ;;
    linux)
        boot_line "sys  " "Linux  ${C_DIM}(full support)${C_RESET}" "$C_GREEN"
        ;;
    macos)
        boot_line "sys  " "macOS  ${C_DIM}(full support)${C_RESET}" "$C_GREEN"
        ;;
    windows_bash)
        boot_line "sys  " "${C_RED}Windows (Git Bash / MSYS2)${C_RESET} — WSL2 is strongly recommended" "$C_RED"
        printf "\n  Some features (viz port-forward, shell tool) may not work correctly.\n"
        printf "  Continue anyway? (y/N): "
        read -r _SYS_CONT || _SYS_CONT=""
        if [ "${_SYS_CONT:-n}" != "y" ] && [ "${_SYS_CONT:-n}" != "Y" ]; then
            printf "\n  Exiting. Install WSL2 and re-run from a WSL terminal.\n\n"
            exit 1
        fi
        ;;
    *)
        boot_line "sys  " "${C_GOLD}unknown system (uname=$(uname -s 2>/dev/null)) — proceeding with caution${C_RESET}" "$C_GOLD"
        ;;
esac

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

# ---------- 3b. detect WSL & record shell defaults ----------------------- #
# If we're running inside WSL, or if wsl.exe is reachable from this shell
# (Windows + Git-Bash style invocation), record it under cfg["shell"] so the
# agent's Shell tool can default its WSLExecutor to the right distro/user.

WSL_AVAILABLE="0"
WSL_DEFAULT_DISTRO=""
WSL_DEFAULT_USER=""

if [ -n "${WSL_DISTRO_NAME:-}" ]; then
    WSL_AVAILABLE="1"
    WSL_DEFAULT_DISTRO="$WSL_DISTRO_NAME"
    WSL_DEFAULT_USER="${USER:-}"
elif grep -qi microsoft /proc/version 2>/dev/null; then
    WSL_AVAILABLE="1"
    WSL_DEFAULT_USER="${USER:-}"
elif command -v wsl.exe >/dev/null 2>&1; then
    WSL_AVAILABLE="1"
    # `wsl.exe -l -q` lists distros (one per line, UTF-16 on older Windows).
    # Strip nulls + CRs to coerce to plain ASCII; first non-empty line wins.
    WSL_DEFAULT_DISTRO="$(wsl.exe -l -q 2>/dev/null \
        | tr -d '\000\r' \
        | awk 'NF{print; exit}')"
fi

if [ "$WSL_AVAILABLE" = "1" ]; then
    uv run --quiet python - "$KENT_HOME/config.json" "$WSL_DEFAULT_DISTRO" "$WSL_DEFAULT_USER" <<'PY'
import json, os, sys
p, distro, user = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    cfg = json.loads(open(p).read()) if os.path.exists(p) else {}
except Exception:
    cfg = {}
shell = cfg.get("shell") or {}
shell["wsl_available"] = True
if distro:
    shell["default_distro"] = distro
if user:
    shell["default_user"] = user
cfg["shell"] = shell
os.makedirs(os.path.dirname(p), exist_ok=True)
open(p, "w").write(json.dumps(cfg, indent=2))
PY
    boot_line "wsl  " "detected — distro=${C_BOLD}${WSL_DEFAULT_DISTRO:-<unknown>}${C_RESET} user=${C_BOLD}${WSL_DEFAULT_USER:-<unset>}${C_RESET}" "$C_GREEN"
else
    boot_line "wsl  " "${C_DIM}not detected — shell tool will use host default${C_RESET}" "$C_GOLD"
fi

# ---------- 4. launch viz (background) ----------------------------------- #

if [ "${KENT_NO_VIZ:-0}" = "1" ]; then
    boot_line "viz  " "${C_DIM}skipped (KENT_NO_VIZ=1)${C_RESET}" "$C_GOLD"
else
    # Default bind: 0.0.0.0 on WSL (so the LAN can reach it through the
    # Windows portproxy we set up below); 127.0.0.1 elsewhere.
    if [ -n "${KENT_VIZ_HOST:-}" ]; then
        VIZ_HOST="$KENT_VIZ_HOST"
    elif is_wsl; then
        VIZ_HOST="0.0.0.0"
    else
        VIZ_HOST="127.0.0.1"
    fi

    # Kill any stale viz process that's still holding the port from a previous run.
    STALE_VIZ_PID="$(lsof -ti :"$VIZ_PORT" 2>/dev/null || true)"
    if [ -n "$STALE_VIZ_PID" ]; then
        boot_line "viz  " "clearing stale process on port ${VIZ_PORT} (pid ${STALE_VIZ_PID})" "$C_GOLD"
        kill "$STALE_VIZ_PID" 2>/dev/null || true
        sleep 0.5
    fi

    boot_line "viz  " "spawning 3D palace viewer on ${VIZ_HOST}:${VIZ_PORT}"
    : > "$VIZ_LOG"
    ( uv run --quiet kent viz --host "$VIZ_HOST" --port "$VIZ_PORT" >> "$VIZ_LOG" 2>&1 ) &
    VIZ_PID=$!

    # Poll until the server binds (or give up after ~30s). Probe via bash's
    # built-in /dev/tcp so this works on minimal images without curl/wget.
    BOOT_OK=""
    for _ in $(seq 1 150); do
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

        # WSL only: forward the port from Windows → WSL so devices on the LAN
        # can reach the viz. Skipped if KENT_NO_LAN=1, host is loopback, or
        # we're not actually on WSL.
        if [ "${KENT_NO_LAN:-0}" != "1" ] && [ "$VIZ_HOST" = "0.0.0.0" ] && is_wsl; then
            WSL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
            if [ -n "$WSL_IP" ]; then
                setup_wsl_lan_forward "$WSL_IP" "$VIZ_PORT" || true
                WIN_LAN_IP="$(powershell.exe -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { \$_.IPAddress -notlike '127.*' -and \$_.IPAddress -notlike '169.254.*' -and \$_.InterfaceAlias -notlike '*WSL*' -and \$_.InterfaceAlias -notlike '*Loopback*' } | Select-Object -First 1).IPAddress" 2>/dev/null | tr -d '\r' | head -1)"
                if [ -n "$WIN_LAN_IP" ]; then
                    boot_line "lan  " "LAN URL: ${C_BOLD}${C_INK}http://${WIN_LAN_IP}:${VIZ_PORT}${C_RESET}" "$C_GREEN"
                fi
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
            printf "  Heartbeat Discord channel id (numeric, blank = DM to bot owner): "
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
        block["heartbeat_use_dm"] = False
    except ValueError:
        pass
else:
    block["heartbeat_use_dm"] = True
cfg["gateway"] = block
open(p, "w").write(json.dumps(cfg, indent=2))
PY
        HB_DEST="${HB_CHANNEL_ID:-DM to bot owner}"
        boot_line "hb   " "heartbeat configured: interval=${HB_INTERVAL} channel=${HB_DEST}" "$C_GREEN"
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
    if block.get("heartbeat_use_dm"):
        channel = "DM to bot owner"
    else:
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
    # Kill any stale gateway process left from a previous run.
    STALE_GW_PID=""
    if [ -f "$KENT_HOME/gateway.pid" ]; then
        STALE_GW_PID="$(cat "$KENT_HOME/gateway.pid" 2>/dev/null || true)"
    fi
    if [ -n "$STALE_GW_PID" ] && kill -0 "$STALE_GW_PID" 2>/dev/null; then
        boot_line "gw   " "clearing stale gateway process (pid ${STALE_GW_PID})" "$C_GOLD"
        kill "$STALE_GW_PID" 2>/dev/null || true
        sleep 0.5
    fi

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
