#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════
# PPT-Web 离线部署包打包脚本
#
# 默认构建 linux/arm64 镜像（内网鲲鹏/飞腾等），也可:
#   ARCH=amd64 ./package.sh
#
# 产物:
#   dist-<arch>/ppt-web-<arch>.tar           Docker 镜像
#   dist-<arch>/ppt-web-<arch>-deploy.tar.gz 最终部署包
#   dist-<arch>/install.sh manage.sh docker-compose.yml .env.example SHA256SUMS.txt
# ══════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

ARCH="${ARCH:-arm64}"
case "$ARCH" in
    arm64|aarch64)
        ARCH="arm64"
        PLATFORM="linux/arm64"
        ;;
    amd64|x86_64)
        ARCH="amd64"
        PLATFORM="linux/amd64"
        ;;
    *)
        echo "❌ 不支持的架构: $ARCH（可选 arm64 / amd64）"
        exit 1
        ;;
esac

IMAGE_TAG="ppt-web:${ARCH}"
DIST_DIR="$SCRIPT_DIR/dist-${ARCH}"
IMAGE_FILE="ppt-web-${ARCH}.tar"
PKG_NAME="ppt-web-${ARCH}-deploy.tar.gz"
BUILDER="${BUILDER:-multiarch-builder}"

echo "═══════════════════════════════════════════"
echo "  PPT-Web 离线部署包打包工具"
echo "  架构: ${ARCH} (${PLATFORM})"
echo "═══════════════════════════════════════════"

# ── Step 1: 构建镜像 ──
echo ""
echo "[1/5] 🔨 构建 Docker 镜像..."
BUILD_ARGS=(buildx build --platform "$PLATFORM" -t "$IMAGE_TAG" --load .)
if docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
    BUILD_ARGS=(buildx build --builder "$BUILDER" --platform "$PLATFORM" -t "$IMAGE_TAG" --load .)
fi
docker "${BUILD_ARGS[@]}"
echo "✅ 镜像构建完成: ${IMAGE_TAG}"

FOUND_ARCH="$(docker image inspect --format '{{.Architecture}}' "$IMAGE_TAG")"
echo "🔎 镜像架构: ${FOUND_ARCH}"
if [ "$FOUND_ARCH" != "$ARCH" ]; then
    echo "❌ 镜像架构不符：期望 ${ARCH}，实际 ${FOUND_ARCH}"
    exit 1
fi

# ── Step 2: 导出镜像 ──
echo ""
echo "[2/5] 📦 导出镜像 tar..."
mkdir -p "$DIST_DIR"
rm -f "$DIST_DIR/$IMAGE_FILE"
docker save "$IMAGE_TAG" -o "$DIST_DIR/$IMAGE_FILE"
echo "✅ 镜像已导出: $(du -sh "$DIST_DIR/$IMAGE_FILE" | cut -f1)"

# ── Step 3: 复制部署脚本 ──
echo ""
echo "[3/5] 📋 复制部署文件..."
cp "$SCRIPT_DIR/install.sh"           "$DIST_DIR/install.sh"
cp "$SCRIPT_DIR/manage.sh"            "$DIST_DIR/manage.sh"
cp "$SCRIPT_DIR/docker-compose.yml"   "$DIST_DIR/docker-compose.yml"
cp "$SCRIPT_DIR/.env.example"         "$DIST_DIR/.env.example"
chmod +x "$DIST_DIR/install.sh" "$DIST_DIR/manage.sh"

# ── Step 4: 生成校验和与部署包 ──
echo ""
echo "[4/5] 📦 生成部署包..."
(
    cd "$DIST_DIR"
    sha256sum "$IMAGE_FILE" install.sh manage.sh docker-compose.yml .env.example > SHA256SUMS.txt
)
FINAL="$DIST_DIR/$PKG_NAME"
rm -f "$FINAL"
tar -czf "$FINAL" -C "$DIST_DIR" \
    "$IMAGE_FILE" \
    install.sh \
    manage.sh \
    docker-compose.yml \
    .env.example \
    SHA256SUMS.txt
gzip -t "$FINAL"
tar -tzf "$FINAL" >/dev/null
echo "✅ 部署包: $FINAL ($(du -sh "$FINAL" | cut -f1))"

# ── Step 5: 镜像内容校验（不启动容器） ──
echo ""
echo "[5/5] 🔎 校验镜像内新代码..."
if MSYS_NO_PATHCONV=1 docker run --rm --platform "$PLATFORM" --entrypoint python "$IMAGE_TAG" -c "
from pathlib import Path
assert 'ppt_web_session_v1' in Path('/app/frontend/dist/index.html').read_text(encoding='utf-8')
assert '_persist_task' in Path('/app/backend/app/main.py').read_text(encoding='utf-8')
assert 'def _resolve_page_filename' in Path('/app/backend/app/ppt_engine.py').read_text(encoding='utf-8')
print('image code OK')
"; then
    echo "✅ 镜像内包含前端恢复/后端续跑代码"
else
    echo "⚠️  镜像内容校验未通过（可能是不支持 ${PLATFORM} 模拟运行），请手动检查"
fi

# ── 可选：就地安装测试（不启动服务） ──
if [ "${RUN_INSTALL_TEST:-0}" = "1" ]; then
    echo ""
    echo "🧪 就地安装测试..."
    TEST_DIR="$DIST_DIR/.install-test"
    rm -rf "$TEST_DIR"
    mkdir -p "$TEST_DIR"
    tar -xzf "$FINAL" -C "$TEST_DIR"
    (
        cd "$TEST_DIR"
        ALLOW_ARCH_MISMATCH=1 bash install.sh
        test -f .env
        test -d data/projects
        test -x manage.sh
    )
    rm -rf "$TEST_DIR"
    echo "✅ 就地安装测试通过（无 ~/ppt-web-deploy 复制）"
fi

echo ""
echo "═══════════════════════════════════════════"
echo "  ✅ 打包完成!"
echo "  目录: $DIST_DIR"
echo "  部署包: $PKG_NAME"
echo ""
echo "  内网部署方法（解压目录内直接安装运行）:"
echo "    mkdir -p ppt-web-deploy"
echo "    tar -xzf $PKG_NAME -C ppt-web-deploy"
echo "    cd ppt-web-deploy"
echo "    chmod +x install.sh manage.sh"
echo "    ./install.sh          # 加载镜像、生成 .env 和 data 目录"
echo "    vi .env               # 填写 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL"
echo "    ./manage.sh start     # 启动"
echo ""
echo "  也可安装后立即启动: ./install.sh --start"
echo "═══════════════════════════════════════════"
