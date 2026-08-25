#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CAMERA_VIZ_DIR="${CAMERA_VIZ_DIR:-}"
CAMERA_CONFIG="${CAMERA_CONFIG:-$ROOT/cloudxr/camera_viz_zed_60fps.yaml}"
CLOUDXR_HOME="${CLOUDXR_HOME:-$HOME/.cloudxr}"
QUEST_WAIT_SECONDS="${QUEST_WAIT_SECONDS:-0}"

if [[ -z "$CAMERA_VIZ_DIR" ]]; then
  echo "Set CAMERA_VIZ_DIR to the IsaacTeleop examples/camera_viz directory." >&2
  exit 2
fi

QCRT_PY="$ROOT/.venv/bin/python"
CLOUDXR_PY="$CAMERA_VIZ_DIR/.venv/bin/python"
CAMERA_VIZ="$CAMERA_VIZ_DIR/camera_viz.sh"
CERT="$CLOUDXR_HOME/certs/server.crt"
KEY="$CLOUDXR_HOME/certs/server.key"

for path in "$QCRT_PY" "$CLOUDXR_PY" "$CAMERA_VIZ" "$CAMERA_CONFIG" "$CERT" "$KEY"; do
  [[ -e "$path" ]] || { echo "Missing required path: $path" >&2; exit 2; }
done
[[ "$QUEST_WAIT_SECONDS" =~ ^[0-9]+$ ]] || {
  echo "QUEST_WAIT_SECONDS must be a non-negative integer." >&2
  exit 2
}

cloudxr_signin_count() {
  grep -hF "/sign_in?peer_id=" "$CLOUDXR_HOME"/logs/wss*.log 2>/dev/null \
    | grep -c "Proxying " || true
}

"$QCRT_PY" "$ROOT/scripts/prepare_cloudxr_client.py" >/dev/null
"$CLOUDXR_PY" -c 'import cupy, isaacteleop, pyzed.sl'

if [[ "${1:-}" == "--check" ]]; then
  echo "CloudXR + ZED preflight PASS"
  echo "  camera_viz: $CAMERA_VIZ_DIR"
  echo "  config:     $CAMERA_CONFIG"
  echo "  web client: $ROOT/.cloudxr-client"
  exit 0
fi

if [[ $# -ne 0 ]]; then
  echo "Usage: CAMERA_VIZ_DIR=/path/to/examples/camera_viz $0 [--check]" >&2
  exit 2
fi

signins_before="$(cloudxr_signin_count)"
"$CLOUDXR_PY" -m isaacteleop.cloudxr.service stop >/dev/null 2>&1 || true
TELEOP_WEB_CLIENT_STATIC_DIR="$ROOT/.cloudxr-client" \
  "$CLOUDXR_PY" -m isaacteleop.cloudxr.service start --host-client

qcrt_pid=""
cleanup() {
  if [[ -n "$qcrt_pid" ]] && kill -0 "$qcrt_pid" 2>/dev/null; then
    kill -INT "$qcrt_pid" 2>/dev/null || true
    wait "$qcrt_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

POSE_CERT_FILE="$CERT" \
POSE_KEY_FILE="$KEY" \
POSE_LOG_ENABLED="${POSE_LOG_ENABLED:-0}" \
  "$QCRT_PY" "$ROOT/server.py" &
qcrt_pid=$!

echo "Primary entry: https://<PC-LAN-IP>:8000/ (pose is always available)."
echo "Enable video there to open CloudXR; waiting for that optional connection..."
quest_connected=false
for ((second = 0; ; second += 1)); do
  if (( $(cloudxr_signin_count) > signins_before )); then
    quest_connected=true
    break
  fi
  if ((QUEST_WAIT_SECONDS > 0 && second + 1 >= QUEST_WAIT_SECONDS)); then
    break
  fi
  sleep 1
done
if [[ "$quest_connected" != true ]]; then
  echo "Timed out waiting for Quest on TCP 48322." >&2
  exit 1
fi

if ! "$CAMERA_VIZ" run "$CAMERA_CONFIG"; then
  echo "Video path stopped; pose service remains available on port 8000." >&2
fi
wait "$qcrt_pid"
