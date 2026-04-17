#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  cat <<'EOF'
Usage:
  ./scripts/prepare-clone-target.sh <url> [workspace-root]

Example:
  ./scripts/prepare-clone-target.sh https://example.com
EOF
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
workspace_root="${2:-$repo_root}"
target_url="$1"

host="$(node -e "console.log(new URL(process.argv[1]).host.replace(/:/g, '_'))" "$target_url")"
timestamp="$(date '+%Y%m%d-%H%M%S')"
capture_root="$workspace_root/captures/$host/$timestamp"
mirror_dir="$capture_root/mirror"
research_dir="$capture_root/research"

mkdir -p "$mirror_dir" "$research_dir/components"

"$script_dir/run-httrack-mirror.sh" "$target_url" "$mirror_dir"
node "$script_dir/generate-mirror-manifest.mjs" "$mirror_dir" > "$research_dir/mirror-manifest.json"
ln -sfn "$timestamp" "$workspace_root/captures/$host/latest"

cat > "$research_dir/target.json" <<EOF
{
  "url": "$target_url",
  "host": "$host",
  "capturedAt": "$timestamp",
  "mirrorDir": "$mirror_dir",
  "researchDir": "$research_dir"
}
EOF

cat <<EOF
Prepared clone workspace:
  mirror:   $mirror_dir
  research: $research_dir
EOF
