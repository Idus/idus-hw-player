#!/usr/bin/env bash
# Idus HW-player installer  ·  v1.0  ·  build 2026-10-11
# Installs Idus HW-player as a self-contained folder in ~/.local/share/idus-hw-player
# + a systemd service that starts the player every time the machine boots.
# Run as your normal user (it asks for sudo where it needs it):
#   bash install-idus-hw-player.sh
# Safe to run again to update — your password and settings are kept.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/.local/share/idus-hw-player"
BIN="$HOME/.local/bin"
ENV_FILE="$APP/idus-hw-player.env"
SERVICE=/etc/systemd/system/idus-hw-player.service
ME="$(id -un)"

if [ "$(id -u)" -eq 0 ]; then
    echo "❌ Run this as your normal user, not with sudo — it asks for sudo itself where needed."
    exit 1
fi
[ -f "$SRC/idus-hw-player.py" ] || { echo "❌ idus-hw-player.py not found next to this script."; exit 1; }
command -v sudo >/dev/null 2>&1 || { echo "❌ sudo is missing."; exit 1; }

echo "· installing mpv (asks for your sudo password)"
sudo apt-get update -q
sudo apt-get install -y -q mpv python3

# App + icon + settings in one folder (one folder backup captures everything)
mkdir -p "$APP" "$BIN"
install -m 755 "$SRC/idus-hw-player.py" "$APP/idus-hw-player.py"
if [ -f "$SRC/idus-hw-player.png" ]; then
    install -m 644 "$SRC/idus-hw-player.png" "$APP/idus-hw-player.png"
else
    echo "⚠️  no idus-hw-player.png next to the script — the web GUI runs without its icon."
fi

# Env file: password + paths, read by the service and by the CLI flags. Kept on re-install.
NEW_PASSWORD=""
if [ ! -f "$ENV_FILE" ]; then
    PASSWORD="${HW_PASSWORD:-}"
    if [ -z "$PASSWORD" ] && [ -t 0 ]; then
        read -rsp "Choose a password for the web GUI (Enter = generate one): " PASSWORD
        echo
    fi
    case "$PASSWORD" in
        *"'"* | *"\\"*) echo "❌ The password can't contain ' or \\ — run the installer again."; exit 1 ;;
    esac
    if [ -z "$PASSWORD" ]; then
        PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(9))')"
        NEW_PASSWORD="$PASSWORD"
    fi
    (
        umask 077
        cat > "$ENV_FILE" <<EOF
# Idus HW-player — read at start-up by the service and by the CLI flags.
# Change something here, then run:  sudo systemctl restart idus-hw-player
# Pauses, show hours, volume and videos are set in the web GUI instead.

# Password for the web GUI (the user name doesn't matter)
HW_PASSWORD='$PASSWORD'

# Where the videos live, and the web GUI port
HW_VIDEO_DIR=${HW_VIDEO_DIR:-$HOME/HW-videos}
HW_PORT=${HW_PORT:-8080}

# Extra mpv arguments, e.g.  HW_MPV_ARGS="--audio-device=alsa/hdmi:CARD=vc4hdmi0,DEV=0"
HW_MPV_ARGS=
EOF
    )
fi
VIDEO_DIR="$(grep -E '^HW_VIDEO_DIR=' "$ENV_FILE" | tail -n1 | cut -d= -f2- || true)"
VIDEO_DIR="${VIDEO_DIR:-$HOME/HW-videos}"
PORT="$(grep -E '^HW_PORT=' "$ENV_FILE" | tail -n1 | cut -d= -f2- || true)"
PORT="${PORT:-8080}"
mkdir -p "$VIDEO_DIR"

# PATH wrapper, so 'idus-hw-player --next' works from a shell, a shortcut or a Stream Deck
{
    echo '#!/usr/bin/env bash'
    echo "exec python3 \"$APP/idus-hw-player.py\" \"\$@\""
} > "$BIN/idus-hw-player"
chmod 755 "$BIN/idus-hw-player"

# Screen and sound without a desktop session
for g in video render audio; do
    if getent group "$g" >/dev/null; then
        sudo usermod -aG "$g" "$ME"
    fi
done

# The earlier Swedish test version (spokspelaren) would fight over the screen: switch it off
OLD=""
if [ -f /etc/systemd/system/spokspelaren.service ]; then
    echo "· switching off the earlier test version (spokspelaren)"
    sudo systemctl disable --now spokspelaren >/dev/null 2>&1 || true
    OLD=1
fi

echo "· creating the service idus-hw-player"
sudo tee "$SERVICE" >/dev/null <<EOF
[Unit]
Description=Idus HW-player (Idus Halloween Player)
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=$ME
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/python3 $APP/idus-hw-player.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable idus-hw-player >/dev/null 2>&1
sudo systemctl restart idus-hw-player

# A desktop session holds the screen, so the player works best without one
if [ "$(systemctl get-default)" = "graphical.target" ]; then
    echo
    echo "⚠️  This machine boots to a desktop, which keeps the screen from the player."
    ANSWER="n"
    if [ -t 0 ]; then
        read -rp "   Boot to console (no desktop) from now on? [Y/n] " ANSWER
        ANSWER="${ANSWER:-y}"
    fi
    case "$ANSWER" in
        [Yy]*)
            sudo systemctl set-default multi-user.target >/dev/null
            echo "   Done. Reboot to take effect:  sudo reboot"
            echo "   (desktop back later:  sudo systemctl set-default graphical.target)" ;;
        *)
            echo "   Left as is. Change later with:  sudo systemctl set-default multi-user.target" ;;
    esac
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "✅ Done."
echo "   • Web GUI:   http://${IP:-<this-machine>}:$PORT"
[ -n "$NEW_PASSWORD" ] && echo "   • Password:  $NEW_PASSWORD   (stored in $ENV_FILE)"
echo "   • Videos:    $VIDEO_DIR   (or upload them in the web GUI)"
echo "   • CLI:       idus-hw-player --status | --next | --stop | --on | --off"
echo "   • Live log:  journalctl -u idus-hw-player -f"
echo "   • 'idus-hw-player' not found? Log out and in once (~/.local/bin joins your PATH)."
if [ -n "$OLD" ]; then
    echo
    echo "   NOTE: the old 'spokspelaren' install is switched off but left in place. To remove it:"
    echo "     sudo rm -f /etc/systemd/system/spokspelaren.service && sudo systemctl daemon-reload"
    echo "     rm -rf ~/spokspelaren"
    echo "   Your videos from it are still in ~/spoken — move them with:  mv ~/spoken/* \"$VIDEO_DIR\"/"
fi
