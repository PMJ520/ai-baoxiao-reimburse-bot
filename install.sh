#!/usr/bin/env bash
# 费用报销中枢 · 一键安装（macOS / Linux / WSL2）
#
#   curl -fsSL https://raw.githubusercontent.com/PMJ520/ai-baoxiao-reimburse-bot/main/install.sh | bash
#   curl -fsSL ... | bash -s -- --host 192.168.1.100 --port 8000
#   curl -fsSL ... | bash -s -- --host hub.example.com --llm claude --llm-key sk-xxx
#
# 可重复运行：已完成的步骤会跳过，中断后重跑即可接上。
set -euo pipefail

# 必须在任何 cd 之前解析：脚本切到安装目录后再取 dirname $0 会指向错误位置。
# 通过管道执行（curl | bash）时 $0 不是真实路径，此时 SRC_DIR 无效，走网络拉取。
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /nonexistent)"

REPO="${REPO:-PMJ520/ai-baoxiao-reimburse-bot}"
IMAGE_BASE="ghcr.io/${REPO}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/expense-hub}"

HOST=""; PORT=""; TLS=""; VERSION="latest"; ASSUME_YES=0; DO_BUILD=0
LLM_PROVIDER=""; LLM_KEY=""; LLM_BASE=""; LLM_MODEL=""
FEISHU_ID=""; FEISHU_SECRET=""; ADMIN_USER="admin"; ADMIN_PASS=""
DNS_PROVIDER=""; DNS_TOKEN=""

say()  { printf '%s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
die()  { printf '\n错误：%s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,10p' "$0"; exit 0; }

while [ $# -gt 0 ]; do
    case "$1" in
        --host)        HOST="$2"; shift 2 ;;
        --port)        PORT="$2"; shift 2 ;;
        --tls)         TLS="$2"; shift 2 ;;
        --version)     VERSION="$2"; shift 2 ;;
        --dir)         INSTALL_DIR="$2"; shift 2 ;;
        --repo)        REPO="$2"; IMAGE_BASE="ghcr.io/$2"; shift 2 ;;
        --llm)         LLM_PROVIDER="$2"; shift 2 ;;
        --llm-key)     LLM_KEY="$2"; shift 2 ;;
        --llm-base-url) LLM_BASE="$2"; shift 2 ;;
        --llm-model)   LLM_MODEL="$2"; shift 2 ;;
        --feishu-app-id)     FEISHU_ID="$2"; shift 2 ;;
        --feishu-app-secret) FEISHU_SECRET="$2"; shift 2 ;;
        --admin-user)  ADMIN_USER="$2"; shift 2 ;;
        --admin-password) ADMIN_PASS="$2"; shift 2 ;;
        --dns-provider) DNS_PROVIDER="$2"; shift 2 ;;
        --dns-token)   DNS_TOKEN="$2"; shift 2 ;;
        --build)       DO_BUILD=1; shift ;;
        --yes|-y)      ASSUME_YES=1; shift ;;
        -h|--help)     usage ;;
        *) die "未知参数：$1（用 --help 查看用法）" ;;
    esac
done

ask() {
    [ "$ASSUME_YES" = 1 ] && { printf '%s' "${2:-}"; return; }
    local a; read -r -p "$1" a </dev/tty || true
    printf '%s' "${a:-${2:-}}"
}
ask_yn() {
    [ "$ASSUME_YES" = 1 ] && return 0
    local a; read -r -p "$1 [Y/n] " a </dev/tty || true
    [ -z "$a" ] || [ "$a" = y ] || [ "$a" = Y ]
}
# 不用 `tr </dev/urandom | head` —— head 提前关闭管道会让 tr 收到 SIGPIPE，
# 在 pipefail 下整条管道返回 141，脚本会毫无提示地终止。
rand() {
    local n="${1:-24}"
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 $((n * 2)) | LC_ALL=C tr -dc 'A-Za-z0-9' | cut -c1-"$n"
    else
        LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom 2>/dev/null | dd bs="$n" count=1 2>/dev/null
    fi
}

# 传入的是 IP 还是域名——IP 不签证书，纯 HTTP 即可
is_ip_or_local() {
    case "$1" in
        localhost|127.0.0.1|::1) return 0 ;;
        *[!0-9.]*) return 1 ;;
        *) [ "$(printf '%s' "$1" | tr -cd '.' | wc -c)" -eq 3 ] ;;
    esac
}

say "════════════════════════════════════════════"
say "   费用报销中枢 · 安装"
say "════════════════════════════════════════════"

# ---------- 1. 环境 ----------
OS="$(uname -s)"; ARCH="$(uname -m)"
info "系统 $OS/$ARCH"

if ! command -v docker >/dev/null 2>&1; then
    case "$OS" in
        Linux)
            say ""
            info "未检测到 Docker。"
            if ask_yn "  是否自动安装 Docker？"; then
                curl -fsSL https://get.docker.com | sh || die "Docker 安装失败"
                command -v systemctl >/dev/null 2>&1 && sudo systemctl enable --now docker || true
            else
                die "请先安装 Docker 后重试"
            fi ;;
        Darwin)
            die "请先安装 Docker Desktop（brew install --cask docker），
       安装后从启动台打开一次，等菜单栏鲸鱼图标停止转圈，再重新运行本命令。" ;;
        *) die "不支持的系统：$OS" ;;
    esac
fi
docker info >/dev/null 2>&1 || die "Docker 已安装但服务未运行。
       macOS 请从启动台打开 Docker Desktop；Linux 请执行 sudo systemctl start docker。"
docker compose version >/dev/null 2>&1 || die "需要 Docker Compose v2（随新版 Docker 提供）"
info "Docker $(docker info --format '{{.ServerVersion}}') 就绪"

# ---------- 2. 访问方式 ----------
say ""
if [ -z "$HOST" ]; then
    say "  访问方式：填公网域名可自动签 HTTPS；填内网 IP 或留空则用 HTTP。"
    HOST="$(ask '  访问地址（域名或IP，回车=localhost）: ' localhost)"
fi
if is_ip_or_local "$HOST"; then
    MODE="direct"; TLS="${TLS:-none}"
    PORT="${PORT:-8000}"
    URL="http://${HOST}:${PORT}"
    info "按 IP/本地访问 → 纯 HTTP，不签证书"
else
    MODE="proxy"; TLS="${TLS:-auto}"
    URL="https://${HOST}"
    info "按域名访问 → Caddy 自动管理证书（模式 $TLS）"
    # 域名解析预检：不做这步，Caddy 签证书会失败而用户只看到一堆 ACME 报错
    if [ "$TLS" = "auto" ]; then
        RESOLVED="$(getent hosts "$HOST" 2>/dev/null | awk '{print $1}' | head -1 || true)"
        [ -z "$RESOLVED" ] && RESOLVED="$(dig +short "$HOST" 2>/dev/null | tail -1 || true)"
        PUBIP="$(curl -fsS --max-time 6 https://api.ipify.org 2>/dev/null || true)"
        if [ -n "$RESOLVED" ] && [ -n "$PUBIP" ] && [ "$RESOLVED" != "$PUBIP" ]; then
            warn "域名解析到 $RESOLVED，本机公网 IP 是 $PUBIP，两者不一致。"
            warn "Let's Encrypt 需要域名指向本机才能签发证书。"
            warn "内网部署请改用 --tls dns（DNS-01 签真证书）或 --tls none（纯 HTTP）。"
            ask_yn "  仍要继续？" || exit 1
        elif [ -z "$RESOLVED" ]; then
            warn "域名 $HOST 解析不到，证书可能签发失败。"
            ask_yn "  仍要继续？" || exit 1
        else
            info "域名解析校验通过（$RESOLVED）"
        fi
    fi
fi

# ---------- 3. 配置 ----------
say ""
[ -z "$LLM_PROVIDER" ] && LLM_PROVIDER="$(ask '  对话模型 claude/openai（回车跳过，稍后可补）: ' '')"
if [ -n "$LLM_PROVIDER" ] && [ -z "$LLM_KEY" ]; then
    LLM_KEY="$(ask '  模型 API Key: ' '')"
    [ "$LLM_PROVIDER" != "claude" ] && LLM_BASE="$(ask '  模型 Base URL（OpenAI 兼容端点）: ' '')"
fi
[ -z "$FEISHU_ID" ] && FEISHU_ID="$(ask '  飞书 App ID（回车跳过）: ' '')"
[ -n "$FEISHU_ID" ] && [ -z "$FEISHU_SECRET" ] && FEISHU_SECRET="$(ask '  飞书 App Secret: ' '')"

mkdir -p "$INSTALL_DIR/data"
cd "$INSTALL_DIR"

# 凭据只在首次生成，重跑不会把已有密码冲掉
if [ -f .env ]; then
    # shellcheck disable=SC1091
    OLD_TOKEN="$(grep -E '^API_TOKEN=' .env | cut -d= -f2- || true)"
    OLD_PASS="$(grep -E '^ADMIN_PASSWORD=' .env | cut -d= -f2- || true)"
fi
# API Token 仅供前后端接口调用，不对用户展示；每次部署重新生成，
# 降低泄露后的影响面。后台登录用的是密码，与它无关。
API_TOKEN="$(rand 32)"
[ -z "$ADMIN_PASS" ] && ADMIN_PASS="${OLD_PASS:-$(rand 18)}"

IMAGE="${IMAGE_BASE}:${VERSION}"
[ "$DO_BUILD" = 1 ] && IMAGE="expense-hub:local"

cat > .env <<EOF
IMAGE=$IMAGE
DATA_DIR=$INSTALL_DIR/data
HOST_PORT=${PORT:-8000}
TZ=${TZ:-Asia/Shanghai}
API_TOKEN=$API_TOKEN
ADMIN_USER=$ADMIN_USER
ADMIN_PASSWORD=$ADMIN_PASS
LLM_PROVIDER=$LLM_PROVIDER
LLM_API_KEY=$LLM_KEY
LLM_BASE_URL=$LLM_BASE
LLM_MODEL=$LLM_MODEL
FEISHU_APP_ID=$FEISHU_ID
FEISHU_APP_SECRET=$FEISHU_SECRET
DNS_PROVIDER=$DNS_PROVIDER
DNS_API_TOKEN=$DNS_TOKEN
EOF
chmod 600 .env
info "配置已写入 $INSTALL_DIR/.env"

# ---------- 4. 生成 compose 与 Caddyfile ----------
fetch() {   # 优先用本地模板（源码安装），否则从仓库拉
    if [ -f "$SRC_DIR/$1" ]; then cp "$SRC_DIR/$1" "$2"
    else curl -fsSL "https://raw.githubusercontent.com/$REPO/main/$1" -o "$2"; fi
}

if [ "$MODE" = "direct" ]; then
    fetch templates/docker-compose.direct.yml docker-compose.yml
    rm -f Caddyfile
else
    fetch templates/docker-compose.proxy.yml docker-compose.yml
    if [ "$TLS" = "dns" ]; then fetch templates/Caddyfile.dns Caddyfile
    else fetch templates/Caddyfile.domain Caddyfile; fi
    sed -i.bak "s|\${DOMAIN}|$HOST|g; s|\${DNS_PROVIDER}|$DNS_PROVIDER|g; s|\${DNS_API_TOKEN}|$DNS_TOKEN|g" Caddyfile
    rm -f Caddyfile.bak
fi
info "已生成 docker-compose.yml"

# ---------- 5. 启动 ----------
say ""
if [ "$DO_BUILD" = 1 ]; then
    info "本地构建镜像…"
    docker build -t expense-hub:local "$SRC_DIR" || die "镜像构建失败"
else
    info "拉取镜像 $IMAGE …"
    docker compose pull 2>/dev/null || warn "拉取失败，若为私有仓库请先 docker login ghcr.io"
fi

fetch expense-hub expense-hub 2>/dev/null || true
[ -f expense-hub ] && chmod +x expense-hub
docker compose up -d

say ""
info "等待服务就绪…"
HEALTH_URL="http://127.0.0.1:${PORT:-8000}/health"
[ "$MODE" = "proxy" ] && HEALTH_URL="http://127.0.0.1/health"
for i in $(seq 1 40); do
    curl -fsS "$HEALTH_URL" >/dev/null 2>&1 && break
    sleep 3
done

if [ -x ./expense-hub ]; then ./expense-hub info; else
    say ""
    say "────────────────────────────────────────────"
    say "  安装完成"
    say "────────────────────────────────────────────"
    say "  后台地址   $URL/admin"
    say "  用户名     $ADMIN_USER"
    say "  密码       $ADMIN_PASS"
    say "────────────────────────────────────────────"
fi
[ -z "$FEISHU_ID" ] && say "
  提示：还没配飞书应用凭据，IM 通道未启用。
  去飞书开放平台创建企业自建应用，拿到 App ID / Secret 后填入
  $INSTALL_DIR/.env，再执行 ./expense-hub restart 即可。"
