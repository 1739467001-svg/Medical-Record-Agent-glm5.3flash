#!/usr/bin/env bash
# ============================================================
# backup-db.sh · 生产 MySQL 逻辑备份（在服务器上运行）
# 备份 medical_record_agent 库全部表（users/sessions/archives），
# 保留最近 N 份，超出自动清理。建议 crontab 每日执行：
#   0 2 * * * /srv/apps/medical-record-agent/repo/deploy/backup-db.sh >> /root/backup-db.log 2>&1
# ============================================================
set -euo pipefail

APP_DIR="/srv/apps/medical-record-agent"
BACKUP_DIR="/root/backups/mra-db"
KEEP=14

ENV_FILE="$APP_DIR/.llm_env"
[ -f "$ENV_FILE" ] || { echo "缺少 $ENV_FILE"; exit 1; }
get_env() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }

DB_HOST="$(get_env MRA_MYSQL_HOST)"
DB_PORT="$(get_env MRA_MYSQL_PORT)"
DB_USER="$(get_env MRA_MYSQL_USER)"
DB_PASS="$(get_env MRA_MYSQL_PASSWORD)"
DB_NAME="$(get_env MRA_MYSQL_DB)"
[ -x /usr/bin/docker ] && IN_DOCKER=1 || IN_DOCKER=0

mkdir -p "$BACKUP_DIR"
STAMP=$(date +%F_%H%M)
OUT="$BACKUP_DIR/${DB_NAME}_$STAMP.sql.gz"

# 容器内无 mysqldump 时经 host.docker.internal 连宿主（与 auth.py 同参数来源）
if command -v mysqldump >/dev/null 2>&1; then
  MYSQLDUMP="mysqldump -h${DB_HOST:-127.0.0.1} -P${DB_PORT:-3306} -u$DB_USER -p$DB_PASS --single-transaction $DB_NAME"
else
  echo "mysqldump 不可用：请在宿主机安装 mysql-client 或手动导出"; exit 1
fi
$MYSQLDUMP | gzip > "$OUT"
echo "已备份: $OUT ($(du -h "$OUT" | cut -f1))"

ls -1t "$BACKUP_DIR"/${DB_NAME}_*.sql.gz | tail -n +$((KEEP + 1)) | xargs -r rm --
echo "保留最近 $KEEP 份"
