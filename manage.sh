#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════
# PPT-Web 管理脚本
# 用法: ./manage.sh {start|stop|restart|status|logs|update}
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
    if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
            echo "⚠️  PPT-Web 已在运行中"
            return 0
        fi
        echo "🔄 重新启动..."
        docker start "$CONTAINER_NAME"
    else
        echo "🚀 启动 PPT-Web (端口: ${HOST_PORT})..."
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
    if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        echo "⏹  停止 PPT-Web..."
        docker stop "$CONTAINER_NAME"
        echo "✅ 已停止"
    else
        echo "⚠️  PPT-Web 未在运行"
    fi
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
        STATUS="✅ 运行中"
        UPTIME=$(docker inspect --format='{{.State.StartedAt}}' "$CONTAINER_NAME" 2>/dev/null || echo "未知")
        PORT=$(docker port "$CONTAINER_NAME" 8000 2>/dev/null || echo "未映射")
        echo "═══════════════════════════════════════════"
        echo "  PPT-Web 状态: ${STATUS}"
        echo "  启动时间: ${UPTIME}"
        echo "  端口映射: ${PORT}"
        echo "  访问地址: http://<服务器IP>:${HOST_PORT}"
        echo "═══════════════════════════════════════════"
    else
        echo "⚠️  PPT-Web 未运行"
    fi
}

# ── 日志 ──
do_logs() {
    docker logs -f --tail 100 "$CONTAINER_NAME" 2>/dev/null || echo "⚠️  容器不存在"
}

# ── 更新镜像 ──
do_update() {
    echo "⚠️  离线环境更新：请先在联网机器上重新运行 package.sh"
    echo "   然后将新的 ppt-web.tar 拷贝过来执行:"
    echo "   docker load -i ppt-web.tar"
    echo "   ./manage.sh restart"
}

# ── 清理 ──
do_clean() {
    echo "🗑  清理旧容器和数据..."
    read -p "确认清理所有 PPT-Web 数据？(y/N) " confirm
    [ "$confirm" = "y" ] || { echo "已取消"; return; }
    do_stop 2>/dev/null || true
    docker rm "$CONTAINER_NAME" 2>/dev/null || true
    echo "✅ 清理完成（镜像未删除）"
}

# ── 入口 ──
case "${1:-}" in
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
        echo ""
        echo "  start    启动服务"
        echo "  stop     停止服务"
        echo "  restart  重启服务"
        echo "  status   查看运行状态"
        echo "  logs     实时查看日志"
        echo "  update   更新说明(离线)"
        echo "  clean    清理容器和数据"
        ;;
esac
