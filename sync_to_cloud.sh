#!/bin/bash
# 自动同步 dragon_tracker.md 和 holdings.md 到 GitHub 仓库
# 用法: bash sync_to_cloud.sh
# 建议在本地 Alpha Lab cron 完成后自动调用

set -e

REPO_DIR="$HOME/天才交易员/github-actions-cron"
DATA_DIR="$HOME/天才交易员"

cd "$REPO_DIR"

# 检测是否有变更
CHANGES=0

for f in dragon_tracker.md holdings.md; do
    if ! diff -q "$DATA_DIR/$f" "$REPO_DIR/$f" > /dev/null 2>&1; then
        cp "$DATA_DIR/$f" "$REPO_DIR/$f"
        echo "📝 $f 已更新"
        CHANGES=1
    fi
done

if [ $CHANGES -eq 1 ]; then
    git add dragon_tracker.md holdings.md
    git commit -m "🔄 自动同步: $(TZ='Asia/Shanghai' date '+%m/%d %H:%M')" || true
    git push origin main
    echo "✅ 已推送到云端"
else
    echo "✅ 无变更，跳过"
fi