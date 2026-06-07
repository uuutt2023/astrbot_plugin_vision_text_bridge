#!/usr/bin/env bash
# verify_push.sh — git push 完立刻 verify 真的同步到 GitHub
# 用法: ./verify_push.sh [<local_sha>]
# 返回 0 = 同步成功, 非 0 = 失败

set -e
LOCAL_SHA="${1:-$(git rev-parse HEAD)}"
REMOTE_URL="https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge.git"

echo "=== 本地 HEAD ==="
git log --oneline -1
echo ""

echo "=== 远端 HEAD ==="
REMOTE_SHA=$(git ls-remote "$REMOTE_URL" HEAD | awk '{print $1}')
echo "remote: $REMOTE_SHA"
echo ""

if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
  echo "✅ 同步成功 (sha: ${LOCAL_SHA:0:8})"
  exit 0
else
  echo "❌ 落后"
  echo "  local:  $LOCAL_SHA"
  echo "  remote: $REMOTE_SHA"
  echo ""
  echo "本地比远端新: $(git log --oneline $REMOTE_SHA..HEAD | wc -l) 个 commit"
  git log --oneline $REMOTE_SHA..HEAD
  exit 1
fi
