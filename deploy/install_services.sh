#!/usr/bin/env bash
# Make Elfie a real always-on service: auto-start, auto-restart on crash.
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1. LiveKit server — docker keeps it alive across daemon restarts
docker rm -f elfie-livekit 2>/dev/null || true
docker run -d --name elfie-livekit --restart unless-stopped \
  -p 7880:7880 -p 7881:7881 -p 50000-50100:50000-50100/udp \
  -v "$DIR/../livekit.yaml:/livekit.yaml" \
  livekit/livekit-server --config /livekit.yaml

# 2. Agent + dashboard as user services — fill the templates for THIS machine
REPO="$(cd "$DIR/.." && pwd)"
bash "$DIR/fetch_models.sh"
RUNPATH="$HOME/.npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/mnt/c/Windows/System32:/mnt/c/Windows"
mkdir -p ~/.config/systemd/user
SERVICES="elfie-agent elfie-dashboard"
# Wake listener only if the WSLg mic bridge is present (otherwise run the
# Windows listener instead — see windows/START_HERE.txt).
[ -S /mnt/wslg/PulseServer ] && SERVICES="$SERVICES elfie-wake"
for svc in $SERVICES; do
  sed -e "s|__ELFIE_DIR__|$REPO|g" -e "s|__ELFIE_PATH__|$RUNPATH|g" \
      "$DIR/$svc.service" > ~/.config/systemd/user/"$svc.service"
done
systemctl --user daemon-reload
systemctl --user enable --now $SERVICES
loginctl enable-linger "$USER"   # keep services running without a login session

echo
echo "Done. Check:   systemctl --user status elfie-agent elfie-dashboard"
echo "Logs:          journalctl --user -u elfie-agent -f"
echo
echo "One manual step so Elfie survives Windows reboots: Task Scheduler ->"
echo "  Create Task -> trigger 'At log on' -> action: wsl.exe -e true"
echo "  (this boots WSL at logon; systemd then starts everything above)"
