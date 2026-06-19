#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
HARNESS=${1:-}

if [[ -z "$HARNESS" ]]; then
  echo "Usage: containerized/build_harness_image.sh <opencode|pi|hermes|openclaw>" >&2
  exit 2
fi

TMP_DIR=$(mktemp -d)
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

copy_path() {
  local src=$1
  local dst=$2
  if [[ ! -e "$src" ]]; then
    echo "Missing required path: $src" >&2
    echo "Set the matching environment variable documented in README.md if your harness is installed elsewhere." >&2
    exit 1
  fi
  mkdir -p "$(dirname "$dst")"
  cp -a "$src" "$dst"
}

case "$HARNESS" in
  opencode)
    IMAGE=${IMAGE:-mb12-opencode-isolated}
    DOCKERFILE="$ROOT_DIR/containerized/docker/Dockerfile.opencode"
    copy_path "${OPENCODE_BIN:-$HOME/.opencode/bin/opencode}" "$TMP_DIR/.opencode/bin/opencode"
    ;;
  hermes)
    IMAGE=${IMAGE:-mb12-hermes-isolated}
    DOCKERFILE="$ROOT_DIR/containerized/docker/Dockerfile.hermes"
    copy_path "${HERMES_AGENT_DIR:-$HOME/.hermes/hermes-agent}" "$TMP_DIR/.hermes/hermes-agent"
    ;;
  pi)
    IMAGE=${IMAGE:-mb12-pi-isolated}
    DOCKERFILE="$ROOT_DIR/containerized/docker/Dockerfile.pi"
    copy_path "${PI_NODE_BIN:-$HOME/.nvm/versions/node/v20.20.2/bin/node}" "$TMP_DIR/.nvm/versions/node/v20.20.2/bin/node"
    copy_path "${PI_AGENT_DIR:-$HOME/.nvm/versions/node/v20.20.2/lib/node_modules/@mariozechner/pi-coding-agent}" "$TMP_DIR/.nvm/versions/node/v20.20.2/lib/node_modules/@mariozechner/pi-coding-agent"
    ;;
  openclaw)
    IMAGE=${IMAGE:-mb12-openclaw-isolated}
    DOCKERFILE="$ROOT_DIR/containerized/docker/Dockerfile.openclaw"
    copy_path "${OPENCLAW_NODE_BIN:-$HOME/.nvm/versions/node/v22.22.3/bin/node}" "$TMP_DIR/.nvm/versions/node/v22.22.3/bin/node"
    copy_path "${OPENCLAW_AGENT_DIR:-$HOME/.nvm/versions/node/v22.22.3/lib/node_modules/openclaw}" "$TMP_DIR/.nvm/versions/node/v22.22.3/lib/node_modules/openclaw"
    ;;
  *)
    echo "Unknown harness: $HARNESS" >&2
    exit 2
    ;;
esac

docker build -f "$DOCKERFILE" -t "$IMAGE" "$TMP_DIR"
echo "Built $IMAGE"
