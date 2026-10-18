#!/usr/bin/env bash
# ============================================================
# install.sh · 院内一键安装（无外网环境可用；在交付包根目录执行）
#
# 用法：
#   sudo bash install.sh                 # 标准安装（对接院内已有 MySQL 或暂用降级）
#   sudo bash install.sh --with-mysql    # 启用交付包容器化 MySQL（profile mysql）
#   sudo bash install.sh --app-dir /srv/apps/medical-record-agent
#
# 做什么：
#   [1/6] 环境检查（docker / compose v2 / 磁盘 / 端口）
#   [2/6] 导入离线镜像（images/*.tar）
#   [3/6] 落位文件（code→repo、compose、conf、models）
#   [4/6] 生成 .llm_env（交互式或复用已有；chmod 600）
#   [5/6] docker compose up -d
#   [6/6] 部署验证（容器健康 / 页面 / API /health）
#
# 全程幂等：重复执行只做增量，不覆盖已填写的 .llm_env
# ============================================================
set -euo pipefail

APP_DIR="/srv/apps/medical-record-agent"
WITH_MYSQL=0
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --with-mysql) WITH_MYSQL=1 ;;
    --app-dir) APP_DIR="$2"; shift ;;
    *) echo "未知参数：$1（可用：--with-mysql --app-dir <目录>）"; exit 2 ;;
  esac
  shift
done

step() { echo; echo ">>> [$1] $2"; }
die()  { echo "❌ $1"; exit 1; }

# ---------- [1/6] 环境检查 ----------
step "1/6" "环境检查"
[ "$(id -u)" = 0 ] || die "请以 root 运行：sudo bash install.sh"
command -v docker >/dev/null || die "未安装 Docker（无外网环境请用离线包安装后重试）"
docker compose version >/dev/null 2>&1 || die "需要 Docker Compose v2（docker compose 子命令）"
[ -d "$SRC_DIR/images" ] || die "交付包不完整：缺少 images/ 目录"
FREE_GB=$(df -BG "$(dirname "$APP_DIR")" | awk 'NR==2{gsub("G","");print $4}')
[ "${FREE_GB:-0}" -ge 5 ] || die "磁盘剩余 ${FREE_GB}GB，建议 ≥5GB"
echo "    ✓ docker $(docker --version | grep -o '[0-9.]*' | head -1) · compose v2 · 剩余 ${FREE_GB}GB"

# ---------- [2/6] 导入镜像 ----------
step "2/6" "导入离线镜像"
for tar in "$SRC_DIR"/images/*.tar; do
  [ -f "$tar" ] || die "images/ 下无镜像 tar"
  echo "    docker load < $(basename "$tar")"
  docker load -i "$tar"
done
docker images | grep -E "mra/llm-api|nginx" || die "镜像导入后未检出，请检查 images/"

# ---------- [3/6] 落位文件 ----------
step "3/6" "落位文件 → $APP_DIR"
mkdir -p "$APP_DIR"
[ -d "$SRC_DIR/code" ] || die "交付包不完整：缺少 code/ 目录"
[ -d "$APP_DIR/repo" ] && echo "    · repo/ 已存在，跳过覆盖（更新请用 runbook §8.3）" \
                     || { cp -r "$SRC_DIR/code" "$APP_DIR/repo"; echo "    · code → repo/"; }
cp -f "$SRC_DIR/docker-compose.yml" "$APP_DIR/docker-compose.yml"
mkdir -p "$APP_DIR/conf"
cp -f "$SRC_DIR/nginx/static-default.conf" "$APP_DIR/conf/default.conf"
[ -d "$SRC_DIR/models" ] && { mkdir -p "$APP_DIR/models"; cp -rn "$SRC_DIR/models/." "$APP_DIR/models/"; echo "    · models/（本地 ASR 模型，如有）"; }
echo "    · docker-compose.yml / conf/default.conf"

# ---------- [4/6] 生成 .llm_env ----------
step "4/6" "配置 .llm_env"
ENV_FILE="$APP_DIR/.llm_env"
if [ -f "$ENV_FILE" ]; then
  echo "    · 已存在，保留不覆盖（如需重置：rm 后重跑 install.sh）"
else
  cp "$SRC_DIR/llm_env.template" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  if [ -t 0 ]; then
    echo "    将打开编辑器填写密钥（MySQL 密码 / LLM 密钥 / ASR 引擎）……"
    "${EDITOR:-vi}" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
  else
    echo "    ⚠ 非交互环境：已落模板，请手工编辑 ${ENV_FILE} 后执行："
    echo "      cd ${APP_DIR} && docker compose up -d"
  fi
fi
if [ "$WITH_MYSQL" = 1 ] && grep -qE '^(MRA_MYSQL_PASSWORD|MYSQL_ROOT_PASSWORD)=<' "$ENV_FILE"; then
  die ".llm_env 中 MYSQL_ROOT_PASSWORD/MRA_MYSQL_PASSWORD 仍为占位符，请先填写"
fi

# ---------- [5/6] 启动 ----------
step "5/6" "启动容器"
cd "$APP_DIR"
# compose 默认不读 .llm_env：软链为 .env 供其插值（端口/密钥/限幅变量）
[ -e .env ] || ln -s .llm_env .env
docker compose config --quiet \
  || die "compose 配置校验失败：检查 .llm_env 占位符是否已全部填写（<> 包裹的值）"
if [ "$WITH_MYSQL" = 1 ]; then
  docker compose --profile mysql up -d
else
  docker compose up -d
fi

# ---------- [6/6] 验证 ----------
step "6/6" "部署验证"
# 容器健康首检需 30s+：最多等 90s 再下结论
WAIT=0
while [ "$WAIT" -lt 90 ]; do
  S=$(docker inspect --format "{{.State.Health.Status}}" medical-record-agent 2>/dev/null || echo starting)
  A=$(docker inspect --format "{{.State.Health.Status}}" mra-llm-api 2>/dev/null || echo starting)
  [ "$S" = healthy ] && [ "$A" = healthy ] && break
  WAIT=$((WAIT + 15)); sleep 15
done
ok=1
[ "$S" = healthy ] || { echo "    ✗ 静态容器状态 ${S}（docker logs medical-record-agent 排查）"; ok=0; }
[ "$A" = healthy ] || { echo "    ✗ API 容器状态 ${A}（docker logs mra-llm-api 排查）"; ok=0; }
STATIC_PORT=$(grep -E "^MRA_STATIC_PORT=" "$ENV_FILE" | cut -d= -f2-); STATIC_PORT="${STATIC_PORT:-3904}"
API_PORT=$(grep -E "^MRA_API_PORT=" "$ENV_FILE" | cut -d= -f2-); API_PORT="${API_PORT:-3905}"
curl -s -o /dev/null "http://127.0.0.1:${STATIC_PORT}/index.html" \
  && echo "    ✓ 静态页 http://127.0.0.1:${STATIC_PORT}/index.html → 200" \
  || { echo "    ✗ 静态页不可达"; ok=0; }
curl -s "http://127.0.0.1:${API_PORT}/health" | grep -q '"ok"' \
  && echo "    ✓ API /health 正常" \
  || { echo "    ✗ API /health 异常"; ok=0; }

echo
[ "$ok" = 1 ] && echo "✅ 安装完成。下一步：信息科按 nginx/mra-entry.conf.sample 配置入口反代（说明书 §4.6），再走 §7 验收清单。" \
              || echo "⚠ 有未通过项：按上方提示排查后重跑 sudo bash install.sh（幂等），或查看说明书 §10 FAQ。"
