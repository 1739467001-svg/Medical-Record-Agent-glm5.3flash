#!/usr/bin/env bash
# ============================================================
# verify.sh · 交付包自检 + 部署后自动化验收（对应说明书 §7 的可自动项）
#
# 两种用法：
#   bash verify.sh            # 在交付包根目录：交付包完整性自检（无需 root/docker）
#   bash verify.sh --deployed # 在部署目录 $APP_DIR：部署后自动化验收（需 docker+curl）
# install.sh 最后一步已内置部署验证；本脚本供信息科独立复核与验收留档。
# ============================================================
set -uo pipefail
MODE="package"
DEPLOY_DIR=""
if [ "${1:-}" = "--deployed" ]; then MODE="deployed"; DEPLOY_DIR="${2:-/srv/apps/medical-record-agent}"; fi
PASS=0; FAIL=0
ck() {  # ck <名称> <命令>  —— 命令成功=通过
  if eval "$2" >/dev/null 2>&1; then echo "  ✓ $1"; PASS=$((PASS+1))
  else echo "  ✗ $1"; FAIL=$((FAIL+1)); fi
}

if [ "$MODE" = "package" ]; then
  cd "$(dirname "$0")"   # 自动切到交付包根目录（脚本所在处），任意位置执行均可
  echo "== 交付包完整性自检（对应说明书 §3 交付物清单）=="
  for f in install.sh verify.sh llm_env.template docker-compose.yml \
           nginx/static-default.conf nginx/mra-entry.conf.sample \
           scripts/backup-db.sh SHA256SUMS; do
    ck "存在 $f" "[ -f '$f' ]"
  done
  ck "code/ 代码目录"      "[ -d code/demo ] && [ -d code/server ]"
  ck "code/ 四色演示数据"  "[ -f code/demo/data/patients.json ]"
  ck "docs/ 部署说明书"    "ls docs/01_私有化部署交付说明书*.md >/dev/null 2>&1"
  if ls images/*.tar >/dev/null 2>&1; then
    ck "images/ 离线镜像存在" "ls images/*.tar >/dev/null"
    for t in images/*.tar; do ck "镜像可读 $(basename "$t")" "tar -tf '$t' >/dev/null"; done
  else
    echo "  - images/ 为空（--skip-images 打包）：联调包，不可用于正式交付"
  fi
  ck "无红线音频文件"      "! find . -name '*.wav' -o -name '*.mp3' -o -name '*.m4a' | grep -q ."
  ck "无患者代号映射"      "! find . -name '患者代号映射.json' | grep -q ."
  SUM="shasum -a 256"; command -v shasum >/dev/null 2>&1 || SUM="sha256sum"
  echo "== 校验和复核（应全行 OK）=="
  if [ ! -f SHA256SUMS ]; then
    echo "  ✗ 缺少 SHA256SUMS（打包不完整）"; FAIL=$((FAIL+1))
  elif $SUM -c SHA256SUMS --quiet 2>/dev/null; then
    echo "  ✓ SHA256SUMS 全部一致"; PASS=$((PASS+1))
  else
    echo "  ✗ 校验和不一致（文件被改动或传输损坏）"; FAIL=$((FAIL+1))
  fi
  echo "== 结果：$PASS 通过 / $FAIL 失败 =="
  exit $([ "$FAIL" = 0 ] && echo 0 || echo 1)
fi

# ---------- 部署后验收（说明书 §7 可自动项；人工项见 02_验收测试报告模板）----------
APP_DIR="$DEPLOY_DIR"
[ -d "$APP_DIR" ] || { echo "部署目录不存在：${APP_DIR}（自定义目录：bash verify.sh --deployed <目录>）"; exit 1; }
cd "$APP_DIR"
ck "compose 配置可解析（.env 软链就绪）" "docker compose config --quiet"
echo "== 部署后自动化验收（${APP_DIR}；说明书 §7）=="
ck "§7-2 静态容器 healthy"  "docker inspect --format '{{.State.Health.Status}}' medical-record-agent | grep -q healthy"
ck "§7-2 API 容器 healthy"  "docker inspect --format '{{.State.Health.Status}}' mra-llm-api | grep -q healthy"
ck "§7-2 重启策略自启"      "docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' mra-llm-api | grep -q unless-stopped"
STATIC_PORT=$(grep -E "^MRA_STATIC_PORT=" .llm_env 2>/dev/null | cut -d= -f2-); STATIC_PORT="${STATIC_PORT:-3904}"
API_PORT=$(grep -E "^MRA_API_PORT=" .llm_env 2>/dev/null | cut -d= -f2-); API_PORT="${API_PORT:-3905}"
ck "§7-1 工作台页面可达"    "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:${STATIC_PORT}/index.html | grep -q 200"
ck "API /health ok"         "curl -s http://127.0.0.1:${API_PORT}/health | grep -q '\"ok\"'"
ck "§7-13 密钥文件权限 600" "[ \"\$(stat -c%a .llm_env 2>/dev/null || stat -f%Lp .llm_env)\" = \"600\" ]"
ck "§7-13 .llm_env 不在 Git" "! git -C repo ls-files --error-unmatch ../.llm_env"
ck "ASR 引擎状态接口"       "curl -s http://127.0.0.1:${API_PORT}/asr/status | grep -q '\"ok\"'"
ck "限流护栏生效（429）"    "curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:${API_PORT}/auth/login -d 'x=1' | grep -qE '4(0[0-9]|29)'"
echo "== 结果：$PASS 通过 / $FAIL 失败（人工验收项：账号角色/录音/生成/归档/审签，见 docs/02_验收测试报告模板）=="
exit $([ "$FAIL" = 0 ] && echo 0 || echo 1)
