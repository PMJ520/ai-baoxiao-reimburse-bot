#!/usr/bin/env sh
# 费用报销中台 —— 宿主机 worker（POSIX sh，busybox 也能跑）
#
# 循环：长轮询要活 → 调本机 CLI → 把输出原样送回。
# 只依赖 curl 和你那个 CLI 本身，不需要 python / node / jq。
#
#   ./worker.sh --url http://localhost:8080 --token XXXX [--cli claude]
#
# 环境变量 EH_URL / EH_TOKEN / EH_CLI 同样有效（开机自启用的是这个）。

set -u

URL="${EH_URL:-}"; TOKEN="${EH_TOKEN:-}"; CLI="${EH_CLI:-}"
while [ $# -gt 0 ]; do
    case "$1" in
        --url)   URL="$2";   shift 2 ;;
        --token) TOKEN="$2"; shift 2 ;;
        --cli)   CLI="$2";   shift 2 ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done
URL="${URL%/}"
[ -n "$URL" ]   || { echo "缺少 --url" >&2; exit 2; }
[ -n "$TOKEN" ] || { echo "缺少 --token" >&2; exit 2; }

# 没指定就挑一个装了的
if [ -z "$CLI" ]; then
    for c in claude codex; do command -v "$c" >/dev/null 2>&1 && { CLI="$c"; break; }; done
fi
command -v "$CLI" >/dev/null 2>&1 || { echo "找不到 CLI：${CLI:-claude/codex 都没装}" >&2; exit 3; }
command -v curl  >/dev/null 2>&1 || { echo "需要 curl" >&2; exit 3; }

# 版本号要拼进 URL 的 query，必须先洗干净：claude --version 会输出
# "2.1.270 (Claude Code)" 这种带空格和括号的串，原样拼接会让 curl 拒绝该 URL
clean() { printf '%s' "$1" | tr -cd 'A-Za-z0-9._-' | cut -c1-24; }
VER="$(clean "$("$CLI" --version 2>/dev/null | head -n1 | awk '{print $1}')")"
OS="$(clean "$(uname -s 2>/dev/null)")"; ARCH="$(clean "$(uname -m 2>/dev/null)")"
TMP="${TMPDIR:-/tmp}/eh-worker.$$"
mkdir -p "$TMP" || exit 3
trap 'rm -rf "$TMP"' EXIT INT TERM

echo "worker 已启动：$CLI ${VER:-} @ $OS $ARCH → $URL"

# 调用 CLI。新增 CLI 在这里加一个分支即可。
run_cli() {
    _p="$(cat "$TMP/prompt")"
    case "$CLI" in
        claude) claude -p "$_p" ;;
        codex)  codex exec "$_p" ;;
        *)      "$CLI" "$_p" ;;
    esac
}

FAILS=0
while :; do
    CODE="$(curl -sS -m 45 -o "$TMP/prompt" -D "$TMP/hdr" -w '%{http_code}' \
        -H "Authorization: Bearer $TOKEN" \
        "$URL/bridge/jobs/next?wait=30&os=$OS&arch=$ARCH&cli=$CLI&ver=$VER" 2>"$TMP/neterr")"

    case "$CODE" in
        204) FAILS=0; continue ;;                    # 没活，接着等
        200) FAILS=0 ;;
        401|503)
            echo "鉴权失败（HTTP $CODE）：令牌不对或后台还没配置桥接令牌" >&2
            sleep 10; continue ;;
        *)
            FAILS=$((FAILS + 1))
            [ "$FAILS" = 1 ] && echo "连不上后台（HTTP ${CODE:-0}）：$(cat "$TMP/neterr" 2>/dev/null | head -n1)" >&2
            # 退避，最多 30 秒一次，避免后台重启时刷屏
            BACK=$((FAILS * 5)); [ "$BACK" -gt 30 ] && BACK=30
            sleep "$BACK"; continue ;;
    esac

    ID="$(awk 'tolower($1) == "x-job-id:" { print $2 }' "$TMP/hdr" | tr -d '\r\n')"
    [ -n "$ID" ] || { echo "响应缺少 X-Job-Id，跳过" >&2; continue; }

    if run_cli >"$TMP/out" 2>"$TMP/err"; then
        curl -sS -m 30 -X POST --data-binary @"$TMP/out" \
            -H "Authorization: Bearer $TOKEN" -H 'Content-Type: text/plain; charset=utf-8' \
            "$URL/bridge/jobs/$ID/result" >/dev/null
    else
        echo "CLI 执行失败（任务 $ID）" >&2
        head -c 2000 "$TMP/err" >"$TMP/err2" 2>/dev/null || : >"$TMP/err2"
        curl -sS -m 30 -X POST --data-binary @"$TMP/err2" \
            -H "Authorization: Bearer $TOKEN" -H 'Content-Type: text/plain; charset=utf-8' \
            "$URL/bridge/jobs/$ID/result?error=1" >/dev/null
    fi
done
