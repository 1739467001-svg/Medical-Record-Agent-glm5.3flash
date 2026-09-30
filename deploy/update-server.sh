#!/usr/bin/env bash
# ============================================================
# update-server.sh · 本机 → 云服务器 一键部署（绕过 GitHub）
# 背景：①服务器到 GitHub 网络不稳定（DEPLOY.md 2026-09-28 备注）；
#      ②本机 SSH 公钥未在服务器 authorized_keys 中（Permission denied 根因）
# 用法：
#   bash deploy/update-server.sh                 # 默认 root@60.205.204.162
#   bash deploy/update-server.sh root@<IP>       # 指定目标
# 首次运行会要求输入一次服务器密码（ssh-copy-id 装公钥），此后免密。
# ============================================================
set -euo pipefail

HOST="${1:-root@60.205.204.162}"
REMOTE_DIR="/srv/apps/medical-record-agent/repo"
APP_DIR="/srv/apps/medical-record-agent"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo ">>> [1/4] 检查 SSH 免密通道：$HOST"
if ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" true 2>/dev/null; then
  echo "    免密 OK"
else
  echo "    免密不可用 —— 需要输入一次服务器密码以安装本机公钥（之后永久免密）"
  ssh-copy-id -o StrictHostKeyChecking=accept-new "$HOST"
  ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" true \
    || { echo "❌ 公钥安装后仍不可免密：请检查服务器 sshd_config 的 PermitRootLogin（应为 prohibit-password 或 yes），"
         echo "   或经阿里云控制台 VNC 登录，将本机公钥追加到 /root/.ssh/authorized_keys："; cat ~/.ssh/id_ed25519.pub; exit 1; }
fi

echo ">>> [2/4] rsync 同步代码（增量，绕过 GitHub）"
ssh "$HOST" "mkdir -p $REMOTE_DIR"
rsync -az --delete \
  --exclude '.git' --exclude '.zcode' --exclude '.DS_Store' \
  --exclude 'server/local_dev.db' --exclude '__pycache__' \
  --exclude 'node_modules' --exclude '*.log' --exclude '*.zip' \
  --exclude '9.24*/' --exclude '9.25*/' --exclude '病历资料/' \
  "$REPO_DIR"/ "$HOST:$REMOTE_DIR"/

echo ">>> [3/4] 重启 llm-api 容器（加载新后端）"
ssh "$HOST" "cd $APP_DIR && (docker compose restart llm-api 2>/dev/null || docker compose restart mra-llm-api) && docker compose ps"

echo ">>> [4/4] 健康检查"
ssh "$HOST" "curl -s http://127.0.0.1:3905/health; echo; \
  curl -s -o /dev/null -w '静态站:%{http_code}\n' http://127.0.0.1:3904/index.html; \
  curl -s -o /dev/null -w '认证接口:%{http_code}\n' -X POST http://127.0.0.1:3905/api/auth/login -H 'Content-Type: application/json' -d '{}'"

echo "✅ 部署完成。公网验证：http://60.205.204.162/mra/"
echo "   后续如需 MySQL：见 DEPLOY.md「MySQL（认证数据库）」一节。"
