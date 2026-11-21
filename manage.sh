#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════
# PPT-Web 管理脚本（在部署包解压目录内使用）
# 用法: ./manage.sh {start|stop|restart|status|logs|update|clean}
# ══════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 加载 .env
[ -f .env ] && source .env

CONTAINER_NAME="ppt-web"
IMAGE_NAME="ppt-web:latest"
HOST_PORT="${HOST_PORT:-8000}"

# ── 启动 ──
do_start() {
    if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        echo "⚠️  PPT-Web 已在运行中"
        echo "   访问: http://<服务器IP>:${HOST_PORT}"
        return 0
    fi

    # 存在已停止的旧容器时先删除，避免 docker start 继续使用旧镜像
    if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        echo "🔄 清理旧容器（确保使用最新镜像）..."
        docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    fi

    echo "🚀 启动 PPT-Web (端口: ${HOST_PORT})..."
    if docker compose version &>/dev/null 2>&1 && [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then
        docker compose up -d --force-recreate
    else
        docker run -d \
            --name "$CONTAINER_NAME" \
            --restart unless-stopped \
            --env-file "$SCRIPT_DIR/.env" \
            -p "${HOST_PORT}:8000" \
            -v "$SCRIPT_DIR/data/projects:/app/projects" \
            -v "$SCRIPT_DIR/data/config:/root/.ppt-web" \
            "$IMAGE_NAME"
    fi
    echo "✅ PPT-Web 已启动"
    echo "   访问: http://<服务器IP>:${HOST_PORT}"
}

# ── 停止 ──
do_stop() {
    echo "⏹  停止 PPT-Web..."
    if docker compose version &>/dev/null 2>&1 && [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then
        docker compose down
    else
        docker stop "$CONTAINER_NAME" 2>/dev/null || true
        docker rm "$CONTAINER_NAME" 2>/dev/null || true
    fi
    echo "✅ 已停止"
}

# ── 重启 ──
do_restart() {
    do_stop
    sleep 1
    do_start
}

# ── 状态 ──
do_status() {
    if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        echo "═══════════════════════════════════════════"
        echo "  ✅ PPT-Web 运行中"
        docker ps --filter name="$CONTAINER_NAME" \
            --format "  容器: {{.Names}}\n  镜像: {{.Image}}\n  端口: {{.Ports}}\n  状态: {{.Status}}"
        echo "  访问: http://<服务器IP>:${HOST_PORT}"
        echo "═══════════════════════════════════════════"
    else
        echo "⚠️  PPT-Web 未运行"
    fi
}

# ── 日志 ──
do_logs() {
    docker logs -f --tail 100 "$CONTAINER_NAME" 2>/dev/null || echo "⚠️  容器不存在"
}

# ── 更新说明 ──
do_update() {
    echo "⚠️  离线环境更新："
    echo "   1. 在联网机器上重新运行 ./package.sh 生成新的部署包"
    echo "   2. 把新的 ppt-web-*-deploy.tar.gz 解压到当前目录并覆盖旧镜像 tar"
    echo "   3. 执行 ./install.sh（会自动 load 并把 ppt-web:latest 指向新镜像）"
    echo "   4. 执行 ./manage.sh restart"
}

# ── 清理 ──
do_clean() {
    echo "🗑  清理容器（不会删除 ./data 数据目录）..."
    read -p "确认清理所有 PPT-Web 容器？(y/N) " confirm
    [ "$confirm" = "y" ] || { echo "已取消"; return; }
    do_stop 2>/dev/null || true
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    echo "✅ 清理完成（镜像和数据目录保留）"
}

# ── 入口 ──
case "${1:-status}" in
    start)   do_start   ;;
    stop)    do_stop    ;;
    restart) do_restart ;;
    status)  do_status  ;;
    logs)    do_logs    ;;
    update)  do_update  ;;
    clean)   do_clean   ;;
    *)
        echo "PPT-Web 管理工具"
        echo ""
        echo "用法: $0 {start|stop|restart|status|logs|update|clean}"
        ;;
esac
