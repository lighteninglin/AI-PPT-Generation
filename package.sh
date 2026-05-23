#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════
# PPT-Web 离线部署包打包脚本
# 在有网络的机器上运行，生成 dist/ 目录包含：
#   - ppt-web.tar.gz    (Docker镜像)
#   - install.sh        (安装脚本)
#   - manage.sh         (管理脚本)
#   - docker-compose.yml
#   - .env.example
# ══════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE_NAME="ppt-web"
IMAGE_TAG="latest"
DIST_DIR="$SCRIPT_DIR/dist"

echo "═══════════════════════════════════════════"
echo "  PPT-Web 离线部署包打包工具"
echo "═══════════════════════════════════════════"

# ── Step 1: 构建 Docker 镜像 ──
echo ""
echo "[1/3] 🔨 构建 Docker 镜像..."
cd "$SCRIPT_DIR"
docker build -t ${IMAGE_NAME}:${IMAGE_TAG} . \
    --platform linux/amd64 \
    --build-arg BUILDKIT_INLINE_CACHE=1

echo "✅ 镜像构建完成: ${IMAGE_NAME}:${IMAGE_TAG}"
docker images ${IMAGE_NAME}:${IMAGE_TAG}

# ── Step 2: 导出镜像 ──
echo ""
echo "[2/3] 📦 导出镜像到 tar..."
mkdir -p "$DIST_DIR"
docker save ${IMAGE_NAME}:${IMAGE_TAG} -o "$DIST_DIR/${IMAGE_NAME}.tar"
echo "✅ 镜像已导出: $(du -sh "$DIST_DIR/${IMAGE_NAME}.tar" | cut -f1)"

# ── Step 3: 复制部署脚本 ──
echo ""
echo "[3/3] 📋 复制部署文件..."
cp "$SCRIPT_DIR/install.sh"      "$DIST_DIR/"
cp "$SCRIPT_DIR/manage.sh"       "$DIST_DIR/"
cp "$SCRIPT_DIR/docker-compose.yml" "$DIST_DIR/"
cp "$SCRIPT_DIR/.env.example"    "$DIST_DIR/"
chmod +x "$DIST_DIR/install.sh" "$DIST_DIR/manage.sh"

# 打包
FINAL="$DIST_DIR/ppt-web-deploy.tar.gz"
tar -czf "$FINAL" -C "$DIST_DIR" \
    ${IMAGE_NAME}.tar \
    install.sh \
    manage.sh \
    docker-compose.yml \
    .env.example

SIZE=$(du -sh "$FINAL" | cut -f1)
echo ""
echo "═══════════════════════════════════════════"
echo "  ✅ 打包完成!"
echo "  输出: $FINAL"
echo "  大小: $SIZE"
echo ""
echo "  部署方法:"
echo "    1. 将 ppt-web-deploy.tar.gz 拷贝到目标服务器"
echo "    2. tar -xzf ppt-web-deploy.tar.gz"
echo "    3. chmod +x install.sh && ./install.sh"
echo "═══════════════════════════════════════════"

# ── Step 4: 本地自动部署测试 ──
echo ""
echo "[4/4] 🚀 本地部署测试..."

DEPLOY_DIR="$HOME/ppt-web-deploy"
HOST_PORT="${HOST_PORT:-8000}"

# 停掉旧容器
if docker ps -a --format '{{.Names}}' | grep -q '^ppt-web$'; then
    echo "  ⏹  停止旧容器..."
    docker stop ppt-web 2>/dev/null || true
    docker rm ppt-web 2>/dev/null || true
fi

# 安装到 ~/ppt-web-deploy
mkdir -p "$DEPLOY_DIR/data/projects" "$DEPLOY_DIR/data/config"
cp "$SCRIPT_DIR/manage.sh"           "$DEPLOY_DIR/"
cp "$SCRIPT_DIR/docker-compose.yml"  "$DEPLOY_DIR/"
cp "$SCRIPT_DIR/.env.example"        "$DEPLOY_DIR/"
if [ ! -f "$DEPLOY_DIR/.env" ]; then
    cp "$SCRIPT_DIR/.env.example" "$DEPLOY_DIR/.env"
fi
chmod +x "$DEPLOY_DIR/manage.sh"

# 加载最新镜像
echo "  📦 加载镜像..."
docker load -q -i "$DIST_DIR/${IMAGE_NAME}.tar"

# 启动
echo "  🚀 启动容器 (端口: ${HOST_PORT})..."
docker run -d \
    --name ppt-web \
    --restart unless-stopped \
    --env-file "$DEPLOY_DIR/.env" \
    -p "${HOST_PORT}:8000" \
    -v "$DEPLOY_DIR/data/projects:/app/projects" \
    -v "$DEPLOY_DIR/data/config:/root/.ppt-web" \
    ${IMAGE_NAME}:${IMAGE_TAG}

# 等待启动
echo "  ⏳ 等待服务就绪..."
for i in $(seq 1 15); do
    if curl -sf "http://localhost:${HOST_PORT}/api/health" | grep -q '"ok"' 2>/dev/null; then
        echo "  ✅ 服务已启动!"
        echo ""
        echo "═══════════════════════════════════════════"
        echo "  🌐 访问: http://localhost:${HOST_PORT}"
        echo "═══════════════════════════════════════════"
        exit 0
    fi
    sleep 1
done

echo "  ⚠️  服务启动超时，请手动检查: docker logs ppt-web"
