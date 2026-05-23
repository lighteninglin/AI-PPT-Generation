#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════
# PPT-Web 离线安装脚本
# 在目标内网服务器上运行（无需互联网连接）
# ══════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE_FILE="$SCRIPT_DIR/ppt-web.tar"
INSTALL_DIR="$HOME/ppt-web-deploy"

echo "═══════════════════════════════════════════"
echo "  PPT-Web 离线安装程序"
echo "═══════════════════════════════════════════"

# ── 检查 Docker ──
check_docker() {
    if ! command -v docker &>/dev/null; then
        echo "❌ 未检测到 Docker，正在尝试安装..."
        if [ -f /etc/os-release ]; then
            . /etc/os-release
            case "$ID" in
                centos|rhel|kylin|neokylin)
                    # 离线环境，尝试本地rpm包
                    echo "⚠️  请先安装 Docker（离线环境建议提前准备好 rpm/deb 包）"
                    echo "   CentOS/Kylin: yum install docker-ce"
                    echo "   然后重新运行此脚本"
                    exit 1
                    ;;
                ubuntu|debian)
                    echo "⚠️  请先安装 Docker: apt install docker.io"
                    exit 1
                    ;;
                *)
                    echo "⚠️  请先安装 Docker"
                    exit 1
                    ;;
            esac
        fi
    fi

    # 检查 docker 是否运行
    if ! docker info &>/dev/null; then
        echo "🔧 启动 Docker 服务..."
        systemctl start docker || service docker start || true
        systemctl enable docker 2>/dev/null || true
    fi
    echo "✅ Docker 就绪"
}

# ── 加载镜像 ──
load_image() {
    if [ ! -f "$IMAGE_FILE" ]; then
        echo "❌ 找不到镜像文件: $IMAGE_FILE"
        exit 1
    fi

    echo "📦 加载 Docker 镜像（可能需要几分钟）..."
    docker load -i "$IMAGE_FILE"
    echo "✅ 镜像加载完成"
    docker images ppt-web:latest
}

# ── 安装 ──
install() {
    echo "📁 创建安装目录: $INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"

    # 复制部署文件
    cp "$SCRIPT_DIR/manage.sh"           "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/docker-compose.yml"  "$INSTALL_DIR/"
    chmod +x "$INSTALL_DIR/manage.sh"

    # 创建 .env（如不存在）
    if [ ! -f "$INSTALL_DIR/.env" ]; then
        cp "$SCRIPT_DIR/.env.example" "$INSTALL_DIR/.env"
        echo "📝 已创建默认配置 .env（请按需修改端口）"
    fi

    # 创建数据持久化目录
    mkdir -p "$INSTALL_DIR/data/projects"
    mkdir -p "$INSTALL_DIR/data/config"

    echo "✅ 安装完成"
}

# ── 完成 ──
print_done() {
    # 读取端口
    PORT=$(grep -E "^HOST_PORT=" "$INSTALL_DIR/.env" 2>/dev/null | cut -d= -f2 || echo "8000")
    echo ""
    echo "═══════════════════════════════════════════"
    echo "  ✅ PPT-Web 安装成功!"
    echo ""
    echo "  启动: cd $INSTALL_DIR && ./manage.sh start"
    echo "  访问: http://<服务器IP>:${PORT}"
    echo ""
    echo "  常用命令:"
    echo "    ./manage.sh start    启动"
    echo "    ./manage.sh stop     停止"
    echo "    ./manage.sh status   查看状态"
    echo "    ./manage.sh logs     查看日志"
    echo "═══════════════════════════════════════════"
}

# ── 执行 ──
check_docker
load_image
install
print_done
