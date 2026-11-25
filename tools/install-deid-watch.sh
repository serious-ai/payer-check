#!/bin/bash
# Installs the hands-off scrub (deid_watch.py) as a launchd agent on this Mac. No AI, no network, no sudo.
#
#   bash tools/install-deid-watch.sh            install or update
#   bash tools/install-deid-watch.sh --test     install, then run a synthetic file through it and show the result
#   bash tools/install-deid-watch.sh --remove   stop it and remove the agent (folders and files are left alone)
#
# After install: drop a raw export in ~/PHI-Work/inbox. Nothing runs until ~/PHI-Work/ARMED exists (one line with
# the date the client's Readiness Notice went out).
set -euo pipefail
LABEL=partners.seriousai.deid-watch
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/Library/Application Support/SeriousAI/deid-watch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ROOT="$HOME/PHI-Work"
OUTBOX="$HOME/Serious-AI/deid-outbox"
PY=/usr/bin/python3

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed the agent. Left in place: $ROOT, $OUTBOX, $APP"
  exit 0
fi

"$PY" -c 'import sys; assert sys.version_info >= (3, 9)' || { echo "Needs Python 3.9+ at $PY (xcode-select --install)"; exit 1; }
mkdir -p "$APP" "$ROOT/inbox" "$ROOT/keys-for-practice" "$ROOT/quarantine" "$OUTBOX" "$HOME/Library/LaunchAgents"
chmod 700 "$ROOT" "$ROOT/inbox" "$ROOT/keys-for-practice" "$ROOT/quarantine"
cp "$SRC/deidentify.py" "$SRC/deid_watch.py" "$APP/"
tmutil addexclusion "$ROOT" 2>/dev/null || echo "Note: couldn't exclude $ROOT from Time Machine; run: tmutil addexclusion ~/PHI-Work"

cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>-I</string><string>$APP/deid_watch.py</string><string>run-once</string>
    <string>--root</string><string>$ROOT</string><string>--outbox</string><string>$OUTBOX</string>
  </array>
  <key>WatchPaths</key><array><string>$ROOT/inbox</string><string>$ROOT/ARMED</string></array>
  <key>StartInterval</key><integer>60</integer>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$ROOT/deid-watch.launchd.log</string>
  <key>StandardErrorPath</key><string>$ROOT/deid-watch.launchd.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed $LABEL (deidentify $(grep -m1 "^VERSION" "$APP/deidentify.py" | cut -d"'" -f2), sha256 $(shasum -a 256 "$APP/deidentify.py" | cut -c1-16))"
echo "Inbox:  $ROOT/inbox"
echo "Outbox: $OUTBOX"
[ -f "$ROOT/ARMED" ] && echo "Armed:  $(cat "$ROOT/ARMED")" || echo "Armed:  NO. Nothing runs until $ROOT/ARMED exists."

if [ "${1:-}" = "--test" ]; then
  # A synthetic report (made-up rows) through a separate test root, so the real folders stay empty.
  T="$(mktemp -d)"
  mkdir -p "$T/root/inbox" "$T/outbox"
  echo "test" > "$T/root/ARMED"
  cp "$SRC/sample-data/deid/sample-cpt-payment-report.csv" "$T/root/inbox/sample-cpt-payment-report.csv"
  touch -t 202601010000 "$T/root/inbox/sample-cpt-payment-report.csv"
  "$PY" -I "$APP/deid_watch.py" run-once --root "$T/root" --outbox "$T/outbox"
  echo "--- outbox:"; ls "$T/outbox"
  echo "--- leak check:"; cat "$T/outbox/"*-leak-check.txt 2>/dev/null || cat "$T/outbox/"*-BLOCKED.txt
  rm -rf "$T"
fi
