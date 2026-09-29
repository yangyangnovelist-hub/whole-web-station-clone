#!/usr/bin/env bash
# One command per machine: export the market data of every known shadow data source on it to the
# private sgk90-shadow-archive repo (one branch `export/<label>` per source), using
# export_to_archive.sh for each. Only market data leaves the machine; see that script's header.
#
#   bash export_all.sh mac       # on the Mac: Aug 23 - Sep 25 local copies, plus the T1 release
#   bash export_all.sh dublin    # on the Dublin server: Sep 21 - 30, database and logs
#
# It skips sources it cannot find, keeps going when one fails, and ends with a checklist.
set -uo pipefail
mode=${1:?usage: bash export_all.sh mac|dublin}
base=https://raw.githubusercontent.com/yangyangnovelist-hub/whole-web-station-clone/claude/polymarket-options-strategy-1lc7i4/research/polymarket-5m/tools
repo=${ARCHIVE_URL:-https://github.com/yangyangnovelist-hub/sgk90-shadow-archive.git}
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || echo /nonexistent)
worker="$here/export_to_archive.sh"
if [ ! -f "$worker" ]; then
  worker=$(mktemp)
  curl -fsSL "$base/export_to_archive.sh" -o "$worker" || { echo "下载 export_to_archive.sh 失败"; exit 1; }
fi

if ! git ls-remote "$repo" >/dev/null 2>&1; then
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then gh auth setup-git >/dev/null 2>&1; fi
  if ! git ls-remote "$repo" >/dev/null 2>&1; then
    echo "这台机器还不能推送到私有仓库。先运行：gh auth login   然后：gh auth setup-git   再重跑本命令。"
    exit 2
  fi
fi

status=()
run() {  # label source [since until]
  local label=$1 src=${2:-}
  if [ -z "$src" ] || [ ! -e "$src" ]; then
    status+=("·  $label：没找到 ${src:-（未定位到文件）}，跳过")
    return
  fi
  echo "==== $label  ←  $src"
  if ARCHIVE_URL="$repo" bash "$worker" "$@"; then
    status+=("✓  $label")
  else
    status+=("✗  $label（报错见上面）")
  fi
}

case "$mode" in
mac)
  D=${DOWNLOADS:-$HOME/Downloads}
  R=$D/sgk90_regime_r553_v3_live/docs
  run aug-0823-0828 "$D/.codex-tmp/sgk90-terminal-12h"
  run payoff-0901-0911 "$R/btcpoly_payoff_research_20260911/data"
  run incident-0901-0911 "$D/sgk90_shadow_incident_backups"
  run holdout-0911-0913 "$R/t1_shadow_loss_anatomy_20260914/drift_20260918/prereg/data_holdout_btc_0911_0913"
  run secB-0913-0915 "$R/t1_shadow_loss_anatomy_20260914/drift_20260918/sec_B.csv.gz"
  run framesB-0913-0915 "$R/t1_shadow_loss_anatomy_20260914/drift_20260918/frames_B.csv.gz"
  if command -v gh >/dev/null 2>&1; then  # T1 (09-14..16) only exists as release assets
    t1=$(mktemp -d)
    if gh release download shadow-t1-20260914-20260916 -R yangyangnovelist-hub/sgk90-shadow-archive -D "$t1" \
         -p runtime_1000.sqlite.bundle.tar.gz -p shadow_current.jsonl.gz; then
      tar xzf "$t1/runtime_1000.sqlite.bundle.tar.gz" -C "$t1" && rm -f "$t1/runtime_1000.sqlite.bundle.tar.gz"
      run t1-0914-0916 "$t1"
    else
      status+=("✗  t1-0914-0916：Release 下载失败")
    fi
    rm -rf "$t1"
  else
    status+=("·  t1-0914-0916：没有 gh，跳过")
  fi
  for f in "$D"/sgk90-regime-reverse-filter-shadow_current_*.jsonl*; do
    [ -e "$f" ] && run "log-$(basename "$f" | grep -o '20[0-9]\{6\}' | head -1)" "$f"
  done
  run pk-archives-0924-0925 "$D/sgk90_shadow_archives"
  run pk-backups-0924-0925 "$D/sgk90_point_kelly_base_shadow_backups"
  ;;
dublin)
  roots="$HOME /opt /srv /data /var/lib"
  db=$(find $roots -maxdepth 6 -name runtime_1000.sqlite -path '*reverse_filter*' 2>/dev/null | head -1)
  svc=${db:+$(dirname "$(dirname "$db")")}
  # the whole service folder: every database (market tables only) and log (market events only) in it
  run dublin-shadow-0921-0930 "$svc" 2026-09-21 2026-09-30
  # engine logs kept outside the service folder
  find $roots -maxdepth 5 -name '*reverse*filter*.jsonl*' -size +20M 2>/dev/null | while read -r f; do
    case "$f" in "$svc"/*) continue ;; esac
    echo "$f"
  done > /tmp/export_logs.txt
  n=0
  while read -r f; do n=$((n + 1)); run "dublin-log-$n-$(basename "$f" | grep -o '20[0-9]\{6\}' | head -1)" "$f" 2026-09-21 2026-09-30
  done < /tmp/export_logs.txt
  find $roots -maxdepth 4 -type d -name '*point_kelly*' 2>/dev/null > /tmp/export_pk.txt
  while read -r d; do run "dublin-pk-$(basename "$d")" "$d" 2026-09-24 2026-09-30; done < /tmp/export_pk.txt
  ;;
*)
  echo "usage: bash export_all.sh mac|dublin"; exit 1 ;;
esac

echo
echo "==== 完成情况 ===="
printf '%s\n' "${status[@]}"
echo "把这张清单发给研究会话即可；✓ 的数据已经在 sgk90-shadow-archive 的 export/* 分支上。"
