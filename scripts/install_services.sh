#!/usr/bin/env bash
# Install or refresh the Scanner user services for this checkout.
#
#   scripts/install_services.sh            install, enable and (re)start
#   scripts/install_services.sh --dry-run  print the units without changing anything
#
# The backend restarts only when no reconstruction is running: a restart would
# interrupt it. Re-run this script once the job has finished.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
units_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
dry_run=false
[[ "${1:-}" == "--dry-run" ]] && dry_run=true

if [[ ! -x "$repo/backend/.venv/bin/uvicorn" ]]; then
  echo "Missing $repo/backend/.venv; create it first (see README, One-time setup)." >&2
  exit 1
fi

render() {
  sed "s|@REPO@|$repo|g" "$repo/deploy/systemd/$1"
}

for unit in scanner-backend.service scanner-job-notifier.service; do
  if $dry_run; then
    echo "# --- $units_dir/$unit"
    render "$unit"
    continue
  fi
  mkdir -p "$units_dir"
  render "$unit" > "$units_dir/$unit.new"
  mv "$units_dir/$unit.new" "$units_dir/$unit"
done
$dry_run && exit 0

systemctl --user daemon-reload
systemctl --user enable scanner-backend.service scanner-job-notifier.service >/dev/null

busy=$(curl -fsS -m 5 "http://127.0.0.1:8000/scans?limit=200" 2>/dev/null | python3 -c '
import json, sys
paused = {"awaiting_box", "awaiting_masks", "awaiting_scope"}
jobs = json.load(sys.stdin)
print(sum(1 for job in jobs
          if job["status"] in ("received", "processing") and job.get("stage") not in paused))
' 2>/dev/null || echo 0)
if [[ "$busy" != "0" ]]; then
  echo "Units installed. $busy job(s) are running, so the backend was not restarted."
  echo "Run this script again when they finish."
  exit 0
fi

systemctl --user restart scanner-backend.service scanner-job-notifier.service
for _ in $(seq 1 20); do
  if curl -fsS -m 2 "http://127.0.0.1:8000/health" >/dev/null 2>&1; then
    address=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo "Scanner is running. Open http://${address:-localhost}:8000 on this network."
    exit 0
  fi
  sleep 1
done
echo "The backend did not answer within 20 seconds; see: journalctl --user -u scanner-backend" >&2
exit 1
