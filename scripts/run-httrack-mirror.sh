#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  cat <<'EOF'
Usage:
  ./scripts/run-httrack-mirror.sh <url> <output-dir> [extra httrack args...]

Example:
  ./scripts/run-httrack-mirror.sh https://example.com ./captures/example.com/latest/mirror
EOF
  exit 1
fi

if ! command -v httrack >/dev/null 2>&1; then
  echo "httrack is required but not installed." >&2
  echo "Install on macOS with: brew install httrack" >&2
  exit 1
fi

target_url="$1"
output_dir="$2"
shift 2

mkdir -p "$output_dir"

echo "Starting HTTrack mirror"
echo "  URL:    $target_url"
echo "  Output: $output_dir"

httrack "$target_url" -O "$output_dir" "$@"

echo "HTTrack mirror completed."
