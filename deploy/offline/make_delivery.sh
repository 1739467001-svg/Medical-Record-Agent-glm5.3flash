#!/usr/bin/env bash
# ============================================================
# make_delivery.sh · 一键生成私有化交付包（乙方有网环境执行）
#
# 产物：
#   dist/mra-delivery-<版本>/        可直接拷贝进院的完整交付包
#     ├── install.sh / verify.sh / llm_env.template / docker-compose.yml
#     ├── images/*.tar               离线镜像（可选导出）
#     ├── code/                     全量代码（红线自动排除，见 [2/8] 扫描）
#     ├── nginx/                    静态容器 conf + 院内入口反代样例
#     ├── docs/                     交付说明书 / 验收模板 / HIS 接口字典 / 对院方三件套
#     ├── models/                   方案B 本地 ASR 模型占位说明
#     └── SHA256SUMS                全包校验和（信息科可现场验证完整性）
#   dist/mra-delivery-<版本>.tar.gz  单文件交付形态
#
# 用法（在仓库根目录）：
#   bash deploy/offline/make_delivery.sh <版本> [选项]
#   版本默认 v1.0；选项：
#     --skip-images      不构建/导出镜像（联调打包跑通用）
#     --with-mysql       附带 mysql:8.0 镜像（院内用自带数据库时才加）
#     --with-local-asr   llm-api 镜像内置 faster-whisper（方案B，体积 +≈1GB）
#     --out <目录>       产物输出目录（默认 dist/）
# ============================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_DIR"

VERSION="v1.0"; SKIP_IMAGES=0; WITH_MYSQL=0; WITH_LOCAL_ASR=0; OUT_ROOT="dist"
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-images)    SKIP_IMAGES=1 ;;
    --with-mysql)     WITH_MYSQL=1 ;;
    --with-local-asr) WITH_LOCAL_ASR=1 ;;
    --out) OUT_ROOT="$2"; shift ;;
    --*) echo "未知选项：$1"; exit 2 ;;
    *) VERSION="$1" ;;
  esac
  shift
done

KIT_SRC="$REPO_DIR/deploy/offline"
OUT="$OUT_ROOT/mra-delivery-$VERSION"
step() { echo; echo ">>> [$1] $2"; }
die()  { echo "❌ $1"; exit 1; }

step "1/8" "准备目录 $OUT"
rm -rf "$OUT"
mkdir -p "$OUT"/{images,nginx,docs,models}
rm -f "$OUT_ROOT/mra-delivery-$VERSION.tar.gz"

step "2/8" "代码落位 code/（红线排除：患者资料/录音/密钥/数据库）"
mkdir -p "$OUT/code"
rsync -a \
  --exclude '.git' --exclude '.gitignore' --exclude '.DS_Store' --exclude '.zcode' \
  --exclude '__pycache__/' --exclude '*.pyc' --exclude 'node_modules/' \
  --exclude '.venv' --exclude 'venv/' --exclude 'tmp/' --exclude 'dist/' \
  --exclude 'gui-test-screenshots/' \
  --exclude '病历资料' --exclude '病历资料.zip' --exclude '患者代号映射.json' \
  --exclude '9.24*/' --exclude '9.25*/' --exclude '*.wav' --exclude '*.mp3' --exclude '*.m4a' --exclude '*.zip' \
  --exclude 'server/local_dev*.db' --exclude '.llm_env' --exclude '*/.llm_env' --exclude '*.log' \
  "$REPO_DIR"/ "$OUT/code/"

step "3/8" "编排/脚本/模板落位"
cp "$KIT_SRC/docker-compose.yml"    "$OUT/docker-compose.yml"
cp "$KIT_SRC/install.sh"            "$OUT/install.sh"
cp "$KIT_SRC/verify.sh"             "$OUT/verify.sh"
cp "$KIT_SRC/llm_env.template"      "$OUT/llm_env.template"
cp "$KIT_SRC/nginx/static-default.conf"      "$OUT/nginx/static-default.conf"
cp "$KIT_SRC/nginx/mra-entry.conf.sample"    "$OUT/nginx/mra-entry.conf.sample"
mkdir -p "$OUT/scripts"
cp "$REPO_DIR/deploy/backup-db.sh"  "$OUT/scripts/backup-db.sh"
cat > "$OUT/models/README.md" <<'EOF'
# models/ · 方案B 本地 ASR 模型目录（说明书 §6.3）
本地 faster-whisper 引擎的模型文件放此处（如 models/whisper-small/），
.llm_env 配 ASR_LOCAL_MODEL=/models/whisper-small 即生效。
模型由乙方在本地评测选定后随包拷入；方案A（云端引擎）时本目录留空即可。
EOF

step "4/8" "文档集 docs/"
cp "$REPO_DIR/docs/私有化交付/01_私有化部署交付说明书_v1.0.md" "$OUT/docs/" 2>/dev/null || echo "    ⚠ 未找到 01_私有化部署交付说明书"
cp "$REPO_DIR/docs/私有化交付/02_验收测试报告模板_v1.0.md"     "$OUT/docs/" 2>/dev/null || true
cp "$REPO_DIR/docs/HIS数据接口说明_8表字典_v1.0.md"            "$OUT/docs/"
cp "$REPO_DIR/docs/对院方推进/01_录音知情同意书模板_v0.1.md"   "$OUT/docs/"
cp "$REPO_DIR/docs/对院方推进/02_HIS对接需求函_v0.1.md"        "$OUT/docs/"

step "5/8" "镜像构建与导出（--skip-images 可跳过）"
if [ "$SKIP_IMAGES" = 1 ]; then
  echo "    已跳过（--skip-images）——生产交付必须去掉此选项"
else
  command -v docker >/dev/null || die "本机无 docker：加 --skip-images 重跑"
  docker info >/dev/null 2>&1 || die "docker daemon 未运行"
  BUILD_ARGS=""
  [ "$WITH_LOCAL_ASR" = 1 ] && BUILD_ARGS="--build-arg WITH_LOCAL_ASR=1"
  echo "    · 构建 mra/llm-api:${VERSION}（含 ffmpeg${WITH_LOCAL_ASR:+ 与 faster-whisper}）"
  docker build -t "mra/llm-api:$VERSION" $BUILD_ARGS -f "$KIT_SRC/Dockerfile.llm-api" "$REPO_DIR"
  docker save "mra/llm-api:$VERSION" -o "$OUT/images/mra-llm-api-$VERSION.tar"
  docker pull public.ecr.aws/nginx/nginx:1.27-alpine
  docker save public.ecr.aws/nginx/nginx:1.27-alpine -o "$OUT/images/nginx-1.27-alpine.tar"
  if [ "$WITH_MYSQL" = 1 ]; then
    docker pull public.ecr.aws/docker/library/mysql:8.0
    docker save public.ecr.aws/docker/library/mysql:8.0 -o "$OUT/images/mysql-8.0.tar"
  fi
fi

step "6/8" "版本清单 VERSION + 快速开始 README"
cat > "$OUT/VERSION" <<EOF
版本：$VERSION
代码：$(git rev-parse HEAD 2>/dev/null || echo unknown)
日期：$(date '+%F %T')
打包机：$(hostname)
EOF
cat > "$OUT/README.md" <<'EOF'
# AI 病历智能体系统 · 私有化交付包 快速开始
1. 校验完整性：与乙方核对 SHA256SUMS（shasum -a 256 -c SHA256SUMS）
2. 安装：sudo bash install.sh --with-mysql   （对接院内已有 MySQL 则去掉 --with-mysql）
3. 验收：bash verify.sh                       （自动化部分对应说明书 §7 清单）
4. 入口：信息科按 nginx/mra-entry.conf.sample 配置院内反代（HTTPS 见 §2.4）
5. 细节：《docs/01_私有化部署交付说明书_v1.0.md》
EOF

step "7/8" "红线扫描（患者数据/密钥零残留，零容忍）"
LEAKS=$(find "$OUT" -type f \( -name '*.wav' -o -name '*.mp3' -o -name '*.m4a' \
  -o -name '.llm_env' -o -name 'local_dev*.db' -o -name '患者代号映射.json' \
  -o -name '*.zip' \) 2>/dev/null; find "$OUT" -type d -name '病历资料' 2>/dev/null)
[ -z "$LEAKS" ] || { echo "❌ 疑似红线文件："; echo "$LEAKS"; exit 1; }
SECRET_HITS=$(find "$OUT/code" "$OUT/docs" -type f \( -name '*.py' -o -name '*.md' -o -name '*.json' \
  -o -name '*.js' -o -name '*.sh' -o -name '*.yml' -o -name '*.template' -o -name '*.sample' -o -name '*.conf' \) \
  -exec grep -lE 'sk-[A-Za-z0-9]{16,}' {} + 2>/dev/null || true)
[ -z "$SECRET_HITS" ] || { echo "❌ 疑似密钥泄漏："; echo "$SECRET_HITS"; exit 1; }
echo "    ✓ 零命中"

step "8/8" "校验和与打包"
SUM="shasum -a 256"; command -v shasum >/dev/null || SUM="sha256sum"
(cd "$OUT" && find . -type f ! -name SHA256SUMS -print0 | xargs -0 $SUM) > "$OUT/SHA256SUMS"
tar -czf "$OUT_ROOT/mra-delivery-$VERSION.tar.gz" -C "$OUT_ROOT" "mra-delivery-$VERSION"

echo
echo "✅ 交付包就绪：$OUT_ROOT/mra-delivery-$VERSION.tar.gz ($(du -sh "$OUT_ROOT/mra-delivery-$VERSION.tar.gz" | cut -f1))"
echo "   目录形态：$OUT/"
[ "$SKIP_IMAGES" = 1 ] && echo "   ⚠ 本次 --skip-images：镜像未导出，不可用于真实交付"
