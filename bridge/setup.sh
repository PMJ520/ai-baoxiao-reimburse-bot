#!/usr/bin/env bash
# 费用报销中台 —— 宿主机 worker 一键安装（macOS / Linux）
#
#   curl -fsSL https://raw.githubusercontent.com/PMJ520/ai-baoxiao-reimburse-bot/main/bridge/setup.sh \
#     | bash -s -- --url http://localhost:8080 --token XXXX
#
# 做四件事：检查依赖 → 认出你装的是哪个 CLI → 装好开机自启 → 起进程。
set -euo pipefail

REPO="${EH_REPO:-PMJ520/ai-baoxiao-reimburse-bot}"
URL=""; TOKEN=""; CLI=""; AUTOSTART=1
HOME_DIR="${HOME:?}"; APP="$HOME_DIR/.expense-hub"

say()  { printf '%s\n' "$*"; }
ok()   { printf '  ✅ %s\n' "$*"; }
warn() { printf '  ⚠️  %s\n' "$*" >&2; }
die()  { printf '\n❌ %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --url)   URL="${2:-}";   shift 2 ;;
        --token) TOKEN="${2:-}"; shift 2 ;;
        --cli)   CLI="${2:-}";   shift 2 ;;
        --no-autostart) AUTOSTART=0; shift ;;
        -h|--help) say "用法：setup.sh --url <后台地址> --token <桥接令牌> [--cli claude] [--no-autostart]"; exit 0 ;;
        *) die "未知参数：$1" ;;
    esac
done
[ -n "$URL" ]   || die "缺少 --url（后台地址，例如 http://localhost:8080）"
[ -n "$TOKEN" ] || die "缺少 --token（在后台「系统设置 → 对话模型」生成）"
URL="${URL%/}"

say ""
say "费用报销中台 · 宿主机 worker 安装"
say "────────────────────────────────"

# ---- 1. 环境检查 ----
say "检查运行环境"
case "$(uname -s)" in
    Darwin) OS=macos;  ok "系统：macOS $(sw_vers -productVersion 2>/dev/null || echo '')" ;;
    Linux)  OS=linux;  ok "系统：Linux $(uname -r)" ;;
    *) die "不支持的系统：$(uname -s)。请改用「通用 / 手动」方式，见 bridge/README.md" ;;
esac
command -v curl >/dev/null 2>&1 || die "缺少 curl。macOS 自带；Linux 请先安装（apt install curl / yum install curl）"
ok "curl 可用"

# ---- 2. 认出 CLI ----
if [ -z "$CLI" ]; then
    for c in claude codex; do
        if command -v "$c" >/dev/null 2>&1; then CLI="$c"; break; fi
    done
fi
if [ -z "$CLI" ] || ! command -v "$CLI" >/dev/null 2>&1; then
    die "没找到可用的 CLI。请先装好并登录其中之一：
     claude  →  https://claude.com/claude-code
     codex   →  https://github.com/openai/codex
   装完重新运行本脚本；也可以用 --cli <命令名> 指定其它 CLI。"
fi
CLI_PATH="$(command -v "$CLI")"
CLI_VER="$("$CLI" --version 2>/dev/null | head -n1 || true)"
ok "CLI：$CLI ${CLI_VER:-} （$CLI_PATH）"

# ---- 3. 落地 worker 脚本 ----
mkdir -p "$APP"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /nonexistent)"
if [ -f "$SELF_DIR/worker.sh" ]; then
    cp "$SELF_DIR/worker.sh" "$APP/worker.sh"            # 从仓库副本安装
else
    curl -fsSL "https://raw.githubusercontent.com/$REPO/main/bridge/worker.sh" \
        -o "$APP/worker.sh" || die "下载 worker.sh 失败，请检查网络或手动下载"
fi
chmod +x "$APP/worker.sh"
ok "worker 脚本：$APP/worker.sh"

# 连通性预检：地址或令牌不对的话，现在就说清楚，别等装完自启才发现
CODE="$(curl -sS -o /dev/null -m 10 -w '%{http_code}' \
    -H "Authorization: Bearer $TOKEN" "$URL/bridge/jobs/next?wait=0" 2>/dev/null || echo 000)"
case "$CODE" in
    200|204) ok "后台连通，令牌有效" ;;
    401) die "令牌不正确。请到后台「系统设置 → 对话模型」核对桥接令牌。" ;;
    503) die "后台尚未设置桥接令牌。请先在后台保存令牌，再运行本脚本。" ;;
    000) die "连不上 $URL 。worker 需要能访问后台地址；跨机器安装时别用 localhost。" ;;
    *)   warn "后台返回 HTTP $CODE，先继续装，装完请到后台点「测试连通性」确认" ;;
esac

# ---- 4. 开机自启 ----
PLIST="$HOME_DIR/Library/LaunchAgents/com.expensehub.worker.plist"
UNIT="$HOME_DIR/.config/systemd/user/expense-hub-worker.service"

install_macos() {
    mkdir -p "$(dirname "$PLIST")"
    cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>com.expensehub.worker</string>
  <key>ProgramArguments</key>
  <array><string>/bin/sh</string><string>$APP/worker.sh</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>EH_URL</key><string>$URL</string>
    <key>EH_TOKEN</key><string>$TOKEN</string>
    <key>EH_CLI</key><string>$CLI</string>
    <key>PATH</key><string>$(dirname "$CLI_PATH"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$APP/worker.log</string>
  <key>StandardErrorPath</key><string>$APP/worker.log</string>
</dict></plist>
PLIST_EOF
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST" || die "launchctl 加载失败，可手动执行：launchctl load $PLIST"
    ok "已装为登录自启（launchd：com.expensehub.worker）"
    STOP="launchctl unload $PLIST"
    LOGS="tail -f $APP/worker.log"
}

install_linux() {
    if ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
        warn "没有 systemd，跳过开机自启"; return 1
    fi
    mkdir -p "$(dirname "$UNIT")"
    cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=Expense Hub CLI worker
After=network-online.target

[Service]
Type=simple
Environment=EH_URL=$URL
Environment=EH_TOKEN=$TOKEN
Environment=EH_CLI=$CLI
Environment=PATH=$(dirname "$CLI_PATH"):/usr/local/bin:/usr/bin:/bin
ExecStart=/bin/sh $APP/worker.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT_EOF
    systemctl --user daemon-reload
    systemctl --user enable --now expense-hub-worker.service || return 1
    # 关掉终端后还要活着，得开 linger，否则退出登录就被杀
    loginctl enable-linger "$(id -un)" >/dev/null 2>&1 \
        || warn "开启 linger 失败，注销后 worker 会停止（需要时手动执行 sudo loginctl enable-linger $(id -un)）"
    ok "已装为开机自启（systemd：expense-hub-worker.service）"
    STOP="systemctl --user disable --now expense-hub-worker.service"
    LOGS="journalctl --user -u expense-hub-worker -f"
    return 0
}

STOP=""; LOGS=""
if [ "$AUTOSTART" = 1 ]; then
    say "配置开机自启"
    if [ "$OS" = macos ]; then install_macos; else install_linux || AUTOSTART=0; fi
fi

if [ "$AUTOSTART" = 0 ]; then
    say ""
    say "未配置自启，请自行在后台运行："
    say "  EH_URL='$URL' EH_TOKEN='***' EH_CLI='$CLI' nohup sh $APP/worker.sh >$APP/worker.log 2>&1 &"
fi

say ""
say "✅ 安装完成"
say "   worker 已在后台运行，去后台「系统设置 → 对话模型」点「测试连通性」验证。"
[ -n "$LOGS" ] && say "   看日志：$LOGS"
[ -n "$STOP" ] && say "   停止它：$STOP"
say ""
