#!/usr/bin/env bash
# =============================================================================
# Passive-Vigilance — Interactive Setup Script
# Guides the user through .env configuration without editing files manually.
# Usage:
#   sudo bash deploy/setup.sh          # Interactive first-time setup
#   sudo bash deploy/setup.sh --show   # Show current config (masked)
#   sudo bash deploy/setup.sh --reset  # Wipe .env and start fresh
# =============================================================================

set -euo pipefail

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"
ENV_EXAMPLE="$REPO_ROOT/.env.example"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

# ── Helpers ───────────────────────────────────────────────────────────────────
info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
header()  { echo -e "\n${BOLD}${CYAN}$*${RESET}"; echo "────────────────────────────────────────"; }

# Mask a value for display: show first 4 chars + ****
mask_value() {
    local val="$1"
    if [[ -z "$val" || "$val" == *"_here"* || "$val" == "your-"* ]]; then
        echo "(not set)"
    elif [[ ${#val} -le 8 ]]; then
        echo "****"
    else
        echo "${val:0:4}****"
    fi
}

# Read current value from .env, or empty string
get_env() {
    local key="$1"
    if [[ -f "$ENV_FILE" ]]; then
        grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"' || true
    fi
}

# Set or update a key=value in .env
set_env() {
    local key="$1"
    local val="$2"
    if grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
        # Replace existing line (BSD and GNU sed compatible)
        sed -i.bak "s|^${key}=.*|${key}=${val}|" "$ENV_FILE" && rm -f "${ENV_FILE}.bak"
    else
        echo "${key}=${val}" >> "$ENV_FILE"
    fi
}

# Prompt for a value with optional default. Blank input keeps default.
# prompt_value "Display label" "ENV_KEY" "default_value" [secret]
prompt_value() {
    local label="$1"
    local key="$2"
    local default="$3"
    local secret="${4:-}"
    local current
    current="$(get_env "$key")"

    # Use current .env value as default if set and not a placeholder
    if [[ -n "$current" && "$current" != *"_here"* && "$current" != "your-"* ]]; then
        default="$current"
    fi

    local display_default
    if [[ -n "$secret" ]]; then
        display_default="$(mask_value "$default")"
    else
        display_default="${default:-none}"
    fi

    local prompt_str
    if [[ -n "$default" ]]; then
        prompt_str="  ${label} [${display_default}]: "
    else
        prompt_str="  ${label}: "
    fi

    local input
    if [[ -n "$secret" ]]; then
        read -rsp "$prompt_str" input
        echo  # newline after silent input
    else
        read -rp "$prompt_str" input
    fi

    # Use typed input or fall back to default
    local result="${input:-$default}"
    set_env "$key" "$result"
    echo "$result"
}

# Yes/no prompt — returns 0 for yes, 1 for no
confirm() {
    local msg="$1"
    local default="${2:-y}"
    local yn
    read -rp "  $msg [${default}]: " yn
    yn="${yn:-$default}"
    [[ "$yn" =~ ^[Yy] ]]
}

# ── --show mode ───────────────────────────────────────────────────────────────
show_config() {
    header "Passive-Vigilance — Current Configuration"
    if [[ ! -f "$ENV_FILE" ]]; then
        warn ".env not found at $ENV_FILE"
        echo "  Run setup.sh without --show to create it."
        exit 0
    fi

    local keys=(
        KISMET_API_KEY WIGLE_API_NAME WIGLE_API_KEY ADSBXLOL_API_KEY
        ALERT_BACKEND NTFY_TOPIC NTFY_SERVER
        TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID DISCORD_WEBHOOK_URL
        WIFI_MONITOR_INTERFACE GPS_DEVICE GPS_DEVICE_TYPE
        KISMET_HOST KISMET_PORT DUMP1090_HOST DUMP1090_PORT
        LOG_LEVEL GUI_ENABLED GUI_PORT GUI_TOKEN
        PERSISTENCE_ALERT_THRESHOLD DRONE_POWER_THRESHOLD_DB
        HANDLE_MAC_RANDOMIZATION IGNORE_RANDOMIZED_MACS
    )

    local secret_keys=(KISMET_API_KEY WIGLE_API_KEY ADSBXLOL_API_KEY
                       TELEGRAM_BOT_TOKEN DISCORD_WEBHOOK_URL GUI_TOKEN)

    for key in "${keys[@]}"; do
        local val
        val="$(get_env "$key")"
        local display
        if printf '%s\n' "${secret_keys[@]}" | grep -qx "$key"; then
            display="$(mask_value "$val")"
        else
            display="${val:-(not set)}"
        fi
        printf "  %-40s %s\n" "$key" "$display"
    done
    echo
}

# ── --reset mode ──────────────────────────────────────────────────────────────
reset_config() {
    header "Reset Configuration"
    warn "This will delete your existing .env file."
    if confirm "Are you sure you want to reset?" "n"; then
        rm -f "$ENV_FILE"
        success ".env deleted. Run setup.sh to reconfigure."
    else
        info "Reset cancelled."
    fi
    exit 0
}

# ── Detect hardware ───────────────────────────────────────────────────────────
detect_wifi_interface() {
    # Prefer wlan1 (USB dongle), avoid wlan0 (built-in — DO NOT USE)
    local iface
    for iface in wlan1 wlan2 wlan3; do
        if iw dev "$iface" info &>/dev/null 2>&1; then
            echo "$iface"
            return
        fi
    done
    echo "wlan1"  # fallback
}

detect_gps_device() {
    # Check for LoRa HAT GNSS on AMA0 first, then USB dongles
    if [[ -e /dev/ttyAMA0 ]]; then
        echo "/dev/ttyAMA0"
    elif [[ -e /dev/ttyUSB0 ]]; then
        echo "/dev/ttyUSB0"
    else
        echo "/dev/ttyUSB0"
    fi
}

# ── Main setup flow ───────────────────────────────────────────────────────────
run_setup() {
    echo
    echo -e "${BOLD}╔══════════════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}║       Passive-Vigilance Setup                ║${RESET}"
    echo -e "${BOLD}║       Counter-Surveillance Sensor Platform   ║${RESET}"
    echo -e "${BOLD}╚══════════════════════════════════════════════╝${RESET}"
    echo

    # ── Seed .env from example if it doesn't exist ────────────────────────────
    if [[ ! -f "$ENV_FILE" ]]; then
        if [[ -f "$ENV_EXAMPLE" ]]; then
            cp "$ENV_EXAMPLE" "$ENV_FILE"
            info "Created .env from .env.example"
        else
            touch "$ENV_FILE"
            warn ".env.example not found — starting with empty config"
        fi
    else
        info "Updating existing .env at $ENV_FILE"
    fi

    # ── Section 1: Required API Keys ─────────────────────────────────────────
    header "1 of 5 — Kismet (Required)"
    echo "  Kismet must be running before generating an API key."
    echo "  Open http://$(hostname -I | awk '{print $1}'):2501 → Settings → API Keys"
    echo
    prompt_value "Kismet API Key" "KISMET_API_KEY" "" secret > /dev/null
    local kismet_key
    kismet_key="$(get_env "KISMET_API_KEY")"
    if [[ -z "$kismet_key" || "$kismet_key" == *"_here"* ]]; then
        warn "Kismet API key not set — Kismet module will be disabled at runtime"
    else
        success "Kismet API key saved"
    fi

    # ── Section 2: Alert Backend ──────────────────────────────────────────────
    header "2 of 5 — Alerts"
    echo "  Choose how Passive-Vigilance sends alerts:"
    echo "    ntfy     — push notifications via ntfy.sh (recommended, free)"
    echo "    telegram — Telegram bot"
    echo "    discord  — Discord webhook"
    echo

    local backend
    backend="$(prompt_value "Alert backend (ntfy/telegram/discord)" "ALERT_BACKEND" "ntfy")"

    case "$backend" in
        ntfy)
            echo
            echo "  Ntfy: create a unique topic name (acts as your private channel)."
            echo "  Use the ntfy app or https://ntfy.sh to subscribe."
            prompt_value "Ntfy topic name" "NTFY_TOPIC" "passive-vigilance-$(hostname -s)" > /dev/null
            prompt_value "Ntfy server URL" "NTFY_SERVER" "https://ntfy.sh" > /dev/null
            success "Ntfy configured"
            ;;
        telegram)
            echo
            echo "  Get a bot token from @BotFather on Telegram."
            echo "  Get your chat ID by messaging @userinfobot."
            prompt_value "Telegram bot token" "TELEGRAM_BOT_TOKEN" "" secret > /dev/null
            prompt_value "Telegram chat ID" "TELEGRAM_CHAT_ID" "" > /dev/null
            success "Telegram configured"
            ;;
        discord)
            echo
            echo "  Create a webhook in your Discord server: Channel Settings → Integrations → Webhooks"
            prompt_value "Discord webhook URL" "DISCORD_WEBHOOK_URL" "" secret > /dev/null
            success "Discord configured"
            ;;
        *)
            warn "Unknown backend '$backend' — defaulting to ntfy"
            set_env "ALERT_BACKEND" "ntfy"
            ;;
    esac

    # ── Section 3: Optional API Keys ─────────────────────────────────────────
    header "3 of 5 — Optional Services"
    echo "  These enhance the platform but are not required to run."
    echo

    if confirm "Configure WiGLE wardriving upload?"; then
        echo
        echo "  Get your API credentials at https://wigle.net → Account → API Token"
        prompt_value "WiGLE API name" "WIGLE_API_NAME" "" > /dev/null
        prompt_value "WiGLE API key" "WIGLE_API_KEY" "" secret > /dev/null
        success "WiGLE configured"
    fi

    echo
    if confirm "Configure adsb.lol ADS-B enrichment?"; then
        echo
        echo "  Free API key available by feeding ADS-B data: https://www.adsb.lol/docs/"
        prompt_value "adsb.lol API key" "ADSBXLOL_API_KEY" "" secret > /dev/null
        success "adsb.lol configured"
    fi

    # ── Section 4: Hardware ───────────────────────────────────────────────────
    header "4 of 5 — Hardware"

    local detected_wifi
    detected_wifi="$(detect_wifi_interface)"
    echo "  Detected WiFi monitor interface: ${detected_wifi}"
    echo "  ${RED}IMPORTANT: Do NOT use wlan0 (built-in). Use the USB dongle only.${RESET}"
    echo
    prompt_value "WiFi monitor interface" "WIFI_MONITOR_INTERFACE" "$detected_wifi" > /dev/null

    local detected_gps
    detected_gps="$(detect_gps_device)"
    echo
    echo "  Detected GPS device: ${detected_gps}"
    if [[ "$detected_gps" == "/dev/ttyAMA0" ]]; then
        info "Waveshare SX126X LoRaWAN/GNSS HAT detected (L76K GNSS on ttyAMA0)"
    fi
    prompt_value "GPS device path" "GPS_DEVICE" "$detected_gps" > /dev/null
    success "Hardware configured"

    # ── Section 5: Advanced / Tuning ─────────────────────────────────────────
    header "5 of 5 — Advanced Settings"
    echo "  Press Enter to accept defaults (recommended for first run)."
    echo

    prompt_value "Log level (DEBUG/INFO/WARNING)" "LOG_LEVEL" "INFO" > /dev/null

    echo
    if confirm "Enable web dashboard GUI?" "n"; then
        set_env "GUI_ENABLED" "true"
        prompt_value "GUI port" "GUI_PORT" "8080" > /dev/null
        echo
        if confirm "Set a bearer token to restrict dashboard access?" "n"; then
            prompt_value "GUI bearer token" "GUI_TOKEN" "" secret > /dev/null
        fi
        success "Web GUI enabled on port $(get_env "GUI_PORT")"
    else
        set_env "GUI_ENABLED" "false"
    fi

    echo
    if confirm "Tune persistence / drone RF thresholds?" "n"; then
        echo
        echo "  Persistence threshold: 0.5=suspicious, 0.7=likely, 0.9=high confidence"
        prompt_value "Persistence alert threshold (0.0-1.0)" "PERSISTENCE_ALERT_THRESHOLD" "0.7" > /dev/null
        echo
        echo "  Drone RF power threshold in dB — signals above this trigger alerts"
        echo "  Typical range: -30 (sensitive) to -10 (less sensitive)"
        prompt_value "Drone RF power threshold (dB)" "DRONE_POWER_THRESHOLD_DB" "-20" > /dev/null
    fi

    # ── Done ──────────────────────────────────────────────────────────────────
    echo
    echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}${GREEN}║   Setup complete!                            ║${RESET}"
    echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════╝${RESET}"
    echo
    info "Config saved to: $ENV_FILE"
    echo
    echo "  Next steps:"
    echo "    sudo systemctl daemon-reload"
    echo "    sudo systemctl enable passive-vigilance"
    echo "    sudo systemctl start passive-vigilance"
    echo "    sudo journalctl -u passive-vigilance -f"
    echo
    echo "  To review your config at any time:"
    echo "    sudo bash deploy/setup.sh --show"
    echo
}

# ── Entrypoint ────────────────────────────────────────────────────────────────
case "${1:-}" in
    --show)   show_config ;;
    --reset)  reset_config ;;
    --help|-h)
        echo "Usage: sudo bash deploy/setup.sh [--show | --reset | --help]"
        echo "  (no args)  Interactive setup / update .env"
        echo "  --show     Print current config with masked secrets"
        echo "  --reset    Delete .env and start fresh"
        exit 0
        ;;
    "")       run_setup ;;
    *)
        error "Unknown option: $1"
        echo "Usage: sudo bash deploy/setup.sh [--show | --reset | --help]"
        exit 1
        ;;
esac
