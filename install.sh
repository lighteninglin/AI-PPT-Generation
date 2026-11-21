#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════
# PPT-Web 离线安装脚本
# 在解压后的部署包目录内直接运行，不再复制到 ~/ppt-web-deploy
#
# 用法:
#   ./install.sh           仅安装（加载镜像、创建 .env 与 data 目录）
#   ./install.sh --start   安装完立即启动服务
# ══════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$SCRIPT_DIR"

# 自动识别部署包内的镜像 tar
if [ -f "$SCRIPT_DIR/ppt-web-arm64.tar" ]; then
    IMAGE_FILE="$SCRIPT_DIR/ppt-web-arm64.tar"
    SOURCE_TAG="ppt-web:arm64"
    EXPECT_ARCH="arm64"
elif [ -f "$SCRIPT_DIR/ppt-web-amd64.tar" ]; then
    IMAGE_FILE="$SCRIPT_DIR/ppt-web-amd64.tar"
    SOURCE_TAG="ppt-web:amd64"
    EXPECT_ARCH="amd64"
elif [ -f "$SCRIPT_DIR/ppt-web.tar" ]; then
    IMAGE_FILE="$SCRIPT_DIR/ppt-web.tar"
    SOURCE_TAG="ppt-web:latest"
    EXPECT_ARCH=""
else
    echo "❌ 找不到镜像文件（ppt-web-arm64.tar / ppt-web-amd64.tar / ppt-web.tar）"
    exit 1
fi

START_AFTER_INSTALL=0
case "${1:-}" in
    --start|-s) START_AFTER_INSTALL=1 ;;
    "") ;;
    *)
        echo "用法: $0 [--start]"
        exit 1
        ;;
esac

echo "═══════════════════════════════════════════"
echo "  PPT-Web 离线安装程序"
echo "═══════════════════════════════════════════"
echo "📁 安装目录: $INSTALL_DIR"

# ── 架构检查 ──
if [ -n "$EXPECT_ARCH" ]; then
    HOST_ARCH="$(uname -m)"
    case "$HOST_ARCH" in
        aarch64|arm64) HOST_ARCH="arm64" ;;
        x86_64|amd64)  HOST_ARCH="amd64" ;;
    esac
    echo "📍 当前架构: ${HOST_ARCH}，镜像架构: ${EXPECT_ARCH}"
    if [ "$HOST_ARCH" != "$EXPECT_ARCH" ]; then
        echo "⚠️  当前架构与镜像架构不一致，运行容器可能报 exec format error。"
        if [ "${ALLOW_ARCH_MISMATCH:-0}" = "1" ]; then
            echo "⚠️  已设置 ALLOW_ARCH_MISMATCH=1，继续安装（仅建议用于测试/模拟环境）"
        elif [ -t 0 ]; then
            read -p "是否继续？(y/N) " confirm
            [ "$confirm" = "y" ] || { echo "已取消"; exit 1; }
        else
            echo "❌ 非交互环境已取消，请换用对应架构的部署包"
            exit 1
        fi
    fi
fi

# ── 检查 Docker ──
check_docker() {
    if ! command -v docker &>/dev/null; then
        echo "❌ 未检测到 Docker"
        if [ -f /etc/os-release ]; then
            . /etc/os-release
            case "$ID" in
                centos|rhel|kylin|neokylin|openEuler)
                    echo "⚠️  请先安装 Docker: yum install docker-ce"
                    ;;
                ubuntu|debian)
                    echo "⚠️  请先安装 Docker: apt install docker.io"
                    ;;
                *)
                    echo "⚠️  请先安装 Docker"
                    ;;
            esac
        fi
        exit 1
    fi

    if ! docker info &>/dev/null; then
        echo "🔧 启动 Docker 服务..."
        systemctl start docker || service docker start || true
        systemctl enable docker 2>/dev/null || true
    fi
    echo "✅ Docker 就绪"
}

# ── 加载镜像 ──
load_image() {
    echo "📦 加载 Docker 镜像（可能需要几分钟）..."
    docker load -i "$IMAGE_FILE"

    # 升级时必须无条件把 :latest 指向本次加载的镜像，
    # 否则旧 latest 会让新镜像不生效。
    docker tag "$SOURCE_TAG" ppt-web:latest
    echo "✅ 镜像加载完成"
    docker images ppt-web
}

# ── 就地安装 ──
install() {
    local missing=0
    for f in manage.sh docker-compose.yml .env.example; do
        if [ ! -f "$SCRIPT_DIR/$f" ]; then
            echo "❌ 部署包缺少文件: $f"
            missing=1
        fi
    done
    [ "$missing" -eq 0 ] || exit 1

    chmod +x "$SCRIPT_DIR/install.sh" "$SCRIPT_DIR/manage.sh" 2>/dev/null || true

    if [ ! -f "$SCRIPT_DIR/.env" ]; then
        cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
        echo "📝 已创建配置 .env（请修改 LLM_API_KEY、LLM_BASE_URL、LLM_MODEL）"
    else
        echo "📝 已保留现有配置 .env"
    fi

    mkdir -p "$SCRIPT_DIR/data/projects" "$SCRIPT_DIR/data/config"
    echo "✅ 安装完成（未向其他目录复制文件）"
}

# ── 完成 ──
print_done() {
    PORT="$(grep -E '^HOST_PORT=' "$SCRIPT_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2 || true)"
    PORT="${PORT:-8000}"
    echo ""
    echo "═══════════════════════════════════════════"
    echo "  ✅ PPT-Web 安装成功!"
    echo ""
    echo "  安装目录: $INSTALL_DIR"
    echo "  访问地址: http://<服务器IP>:${PORT}"
    echo ""
    echo "  启动: ./manage.sh start"
    echo "  一次完成安装并启动: ./install.sh --start"
    echo ""
    echo "  常用命令:"
    echo "    ./manage.sh start    启动"
    echo "    ./manage.sh stop     停止"
    echo "    ./manage.sh status   查看状态"
    echo "    ./manage.sh logs     查看日志"
    echo ""
    echo "  ⚠️  首次启动前请先修改 .env:"
    echo "    LLM_API_KEY    大模型 API 密钥"
    echo "    LLM_BASE_URL   大模型 API 地址"
    echo "    LLM_MODEL      模型名称"
    echo "═══════════════════════════════════════════"
}

check_docker
load_image
install

if [ "$START_AFTER_INSTALL" -eq 1 ]; then
    echo ""
    echo "🚀 启动服务..."
    "$SCRIPT_DIR/manage.sh" start
else
    print_done
fi
