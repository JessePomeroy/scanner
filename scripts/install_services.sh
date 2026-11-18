#!/usr/bin/env bash
# Install or refresh the Scanner user services for this checkout.
#
#   scripts/install_services.sh            install, enable and (re)start
#   scripts/install_services.sh --dry-run  print the units without changing anything
#   scripts/install_services.sh --force    restart even if a job is running
#
# The backend restarts only when it is certain no reconstruction is running: a
# restart would interrupt it. Re-run this script once the job has finished.
set -euo pipefail

usage() {
  sed -n '2,9s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"
}

dry_run=false
force=false
for argument in "$@"; do
  case "$argument" in
    --dry-run) dry_run=true ;;
    --force) force=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $argument" >&2; usage >&2; exit 2 ;;
  esac
done

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
units_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

# The path is substituted into unit files, where these characters change meaning.
if [[ "$repo" =~ [[:space:]\|\&%\$\\\"\'] ]]; then
  echo "This checkout's path contains a space or one of | & % \$ \\ \" ': $repo" >&2
  echo "Move it to a plain path before installing the services." >&2
  exit 1
fi
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
if $dry_run; then
  exit 0
fi

systemctl --user daemon-reload
systemctl --user enable scanner-backend.service scanner-job-notifier.service >/dev/null

# Count jobs doing work. If a running backend cannot be asked, assume it is busy.
running_jobs() {
  # The listing goes through a pipe: as an argument it would exceed the
  # kernel's per-argument limit once a few dozen jobs exist.
  curl -fsS -m 10 "http://127.0.0.1:8000/scans?limit=200" | python3 -c '
import json, sys
paused = {"awaiting_box", "awaiting_masks", "awaiting_scope"}
jobs = json.load(sys.stdin)
print(sum(1 for job in jobs
          if job["status"] in ("received", "processing") and job.get("stage") not in paused))
' 2>/dev/null
}

if systemctl --user is-active --quiet scanner-backend.service && ! $force; then
  if ! busy=$(running_jobs); then
    echo "Units installed, but the running backend did not answer, so it was not restarted." >&2
    echo "Check it, then run this script again (or use --force)." >&2
    exit 1
  fi
  if [[ "$busy" != "0" ]]; then
    echo "Units installed. $busy job(s) are running, so the backend was not restarted."
    echo "Run this script again when they finish."
    exit 0
  fi
fi

systemctl --user restart scanner-backend.service scanner-job-notifier.service
for _ in $(seq 1 20); do
  # Both checks: another process holding the port would answer the second alone.
  if systemctl --user is-active --quiet scanner-backend.service \
      && curl -fsS -m 2 "http://127.0.0.1:8000/health" >/dev/null 2>&1; then
    address=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1 || true)
    echo "Scanner is running. Open http://${address:-localhost}:8000 on this network."
    exit 0
  fi
  sleep 1
done
echo "The backend did not answer within 20 seconds; see: journalctl --user -u scanner-backend" >&2
exit 1
