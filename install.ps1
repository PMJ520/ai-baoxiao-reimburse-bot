# 费用报销中台 · 一键安装（Windows）
#
#   国内：irm https://cnb.cool/hy-team/mj-public/ai-baoxiao-reimburse-bot/-/git/raw/main/install.ps1 | iex
#   海外：irm https://raw.githubusercontent.com/PMJ520/ai-baoxiao-reimburse-bot/main/install.ps1 | iex
#   & ([scriptblock]::Create((irm ...))) -Host 192.168.1.100 -Port 8000
#
# 可重复运行：已完成的步骤会跳过，装完 Docker 重启后重跑即可接上。
param(
    [string]$HostAddr = "",
    [int]$Port = 0,
    [ValidateSet('', 'auto', 'dns', 'none')][string]$Tls = "",
    [string]$Version = "latest",
    [string]$Dir = "$HOME\expense-hub",
    [string]$Repo = "PMJ520/ai-baoxiao-reimburse-bot",
    [string]$Llm = "", [string]$LlmKey = "", [string]$LlmBaseUrl = "", [string]$LlmModel = "",
    [string]$FeishuAppId = "", [string]$FeishuAppSecret = "",
    [string]$AdminUser = "admin", [string]$AdminPassword = "",
    [switch]$Build, [switch]$Yes
)
$ErrorActionPreference = 'Stop'
$SrcDir = if ($PSScriptRoot) { $PSScriptRoot } else { "" }

function Info($m) { Write-Host "  $m" }
function Warn($m) { Write-Host "  ! $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "`n错误：$m" -ForegroundColor Red; exit 1 }
function AskYN($m) { if ($Yes) { return $true }
    $a = Read-Host "$m [Y/n]"; return ($a -eq '' -or $a -match '^[yY]') }
function Ask($m, $d = "") { if ($Yes) { return $d }
    $a = Read-Host $m; if ($a) { return $a } else { return $d } }
function Rand($n = 24) {
    -join ((48..57) + (65..90) + (97..122) | Get-Random -Count $n | ForEach-Object { [char]$_ })
}
# 传入 IP 或 localhost 则纯 HTTP，不签证书
function IsIpOrLocal($h) {
    if ($h -in @('localhost', '127.0.0.1', '::1')) { return $true }
    return ($h -as [ipaddress]) -ne $null
}
# 写入必须用 LF：CRLF 的配置文件挂进 Linux 容器会解析失败，且报错极难定位
function WriteLF($path, $text) {
    [IO.File]::WriteAllText($path, ($text -replace "`r`n", "`n"), [Text.UTF8Encoding]::new($false))
}

Write-Host "════════════════════════════════════════════"
Write-Host "   费用报销中台 · 安装"
Write-Host "════════════════════════════════════════════"
Info "系统 Windows $([Environment]::OSVersion.Version) / $env:PROCESSOR_ARCHITECTURE"

# ---------- 1. Docker ----------
$hasDocker = $null -ne (Get-Command docker -ErrorAction SilentlyContinue)
if (-not $hasDocker) {
    Info "未检测到 Docker Desktop。"
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        if (AskYN "  是否用 winget 安装 Docker Desktop？") {
            winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
            Write-Host ""
            Write-Host "────────────────────────────────────────────"
            Write-Host "  Docker Desktop 已安装，需要重启电脑才能生效。"
            Write-Host ""
            Write-Host "  请按以下三步操作："
            Write-Host "    1. 重启电脑"
            Write-Host "    2. 从开始菜单启动 Docker Desktop，等左下角图标变绿"
            Write-Host "    3. 重新运行本安装命令，会自动接着上次的进度继续"
            Write-Host "────────────────────────────────────────────"
            exit 0
        }
    }
    Die "请先安装 Docker Desktop：https://www.docker.com/products/docker-desktop/"
}
try { docker info *>$null } catch {
    Die "Docker 已安装但未运行。请从开始菜单启动 Docker Desktop，等图标变绿后重试。"
}
if ($LASTEXITCODE -ne 0) {
    Die "Docker 已安装但未运行。请从开始菜单启动 Docker Desktop，等图标变绿后重试。"
}
Info "Docker 就绪"

# ---------- 2. 访问方式 ----------
Write-Host ""
if (-not $HostAddr) {
    Write-Host "  访问方式：填公网域名可自动签 HTTPS；填内网 IP 或留空则用 HTTP。"
    $HostAddr = Ask "  访问地址（域名或IP，回车=localhost）" "localhost"
}
if (IsIpOrLocal $HostAddr) {
    $Mode = "direct"; if (-not $Tls) { $Tls = "none" }
    if ($Port -eq 0) { $Port = 8000 }
    $Url = "http://${HostAddr}:$Port"
    Info "按 IP/本地访问 → 纯 HTTP，不签证书"
} else {
    $Mode = "proxy"; if (-not $Tls) { $Tls = "auto" }
    $Url = "https://$HostAddr"
    Info "按域名访问 → Caddy 自动管理证书（模式 $Tls）"
    if ($Tls -eq 'auto') {
        try {
            $res = (Resolve-DnsName $HostAddr -Type A -ErrorAction Stop |
                    Where-Object { $_.IPAddress } | Select-Object -First 1).IPAddress
            $pub = (Invoke-RestMethod -Uri "https://api.ipify.org" -TimeoutSec 6)
            if ($res -and $pub -and $res -ne $pub) {
                Warn "域名解析到 $res，本机公网 IP 是 $pub，两者不一致。"
                Warn "Let's Encrypt 需要域名指向本机；内网请用 -Tls dns 或 -Tls none。"
                if (-not (AskYN "  仍要继续？")) { exit 1 }
            } else { Info "域名解析校验通过（$res）" }
        } catch { Warn "域名解析校验跳过：$_" }
    }
}

# ---------- 3. 配置 ----------
Write-Host ""
if (-not $Llm) { $Llm = Ask "  对话模型 claude/openai（回车跳过）" "" }
if ($Llm -and -not $LlmKey) {
    $LlmKey = Ask "  模型 API Key" ""
    if ($Llm -ne 'claude') { $LlmBaseUrl = Ask "  模型 Base URL" "" }
}
if (-not $FeishuAppId) { $FeishuAppId = Ask "  飞书 App ID（回车跳过）" "" }
if ($FeishuAppId -and -not $FeishuAppSecret) { $FeishuAppSecret = Ask "  飞书 App Secret" "" }

New-Item -ItemType Directory -Force -Path "$Dir\data" | Out-Null
Set-Location $Dir

# 凭据只在首次生成，重跑不冲掉已有的
$prev = @{}
if (Test-Path "$Dir\creds.json") { $prev = Get-Content "$Dir\creds.json" -Raw | ConvertFrom-Json }
# API Token 仅供接口内部使用，不展示；每次部署重新生成
$ApiToken = Rand 32
if (-not $AdminPassword) {
    $AdminPassword = if ($prev.ADMIN_PASSWORD) { $prev.ADMIN_PASSWORD } else { Rand 18 }
}

$Image = if ($Build) { "expense-hub:local" } else { "ghcr.io/${Repo}:$Version" }

# Windows 不写 .env，改用用户级环境变量（存注册表，仅本用户可读、不会误提交进 git）
$vars = @{
    IMAGE = $Image; DATA_DIR = "$Dir\data"; HOST_PORT = "$Port"; TZ = "Asia/Shanghai"
    API_TOKEN = $ApiToken; ADMIN_USER = $AdminUser; ADMIN_PASSWORD = $AdminPassword
    LLM_PROVIDER = $Llm; LLM_API_KEY = $LlmKey; LLM_BASE_URL = $LlmBaseUrl; LLM_MODEL = $LlmModel
    FEISHU_APP_ID = $FeishuAppId; FEISHU_APP_SECRET = $FeishuAppSecret
}
foreach ($k in $vars.Keys) {
    [Environment]::SetEnvironmentVariable("EH_$k", $vars[$k], 'User')   # 持久化
    Set-Item -Path "Env:$k" -Value $vars[$k]                            # 当前进程立即可用
}
# 用户级变量要新开终端才刷新，故上面同时设了进程级，否则接下来的 compose 取不到值
$vars | ConvertTo-Json | Set-Content "$Dir\creds.json" -Encoding UTF8
Info "配置已写入用户环境变量（备份于 $Dir\creds.json）"

# ---------- 4. compose 与 Caddyfile ----------
# 三个源依次试。raw.githubusercontent.com 在国内时通时不通，只认它必然坑人。
# CNB 的 raw 路径是 /-/git/raw/，写成 /-/raw/ 会返回网页外壳——HTTP 200、
# 内容却是一整页 HTML，光看有没有报错分辨不出来，所以下面要查内容。
$CnbRaw = 'https://cnb.cool/hy-team/mj-public/ai-baoxiao-reimburse-bot/-/git/raw/main'

function Fetch($rel, $dest) {
    if ($SrcDir -and (Test-Path "$SrcDir\$rel")) {
        WriteLF $dest (Get-Content "$SrcDir\$rel" -Raw); return
    }
    $urls = @(
        "$CnbRaw/$rel",
        "https://raw.githubusercontent.com/$Repo/main/$rel",
        "https://cdn.jsdelivr.net/gh/$($Repo.ToLower())@main/$rel"
    )
    foreach ($u in $urls) {
        try {
            $body = Invoke-RestMethod $u -TimeoutSec 30
            if ($body -is [string] -and $body -match '(?i)^\s*<(!DOCTYPE|html)') { continue }
            WriteLF $dest $body
            return
        } catch { }
    }
    throw "下载 $rel 失败，已尝试 $($urls.Count) 个源。请检查网络，或克隆仓库后本地安装。"
}
if ($Mode -eq 'direct') {
    Fetch "templates/docker-compose.direct.yml" "$Dir\docker-compose.yml"
    Remove-Item "$Dir\Caddyfile" -ErrorAction SilentlyContinue
} else {
    Fetch "templates/docker-compose.proxy.yml" "$Dir\docker-compose.yml"
    $cf = if ($Tls -eq 'dns') { "templates/Caddyfile.dns" } else { "templates/Caddyfile.domain" }
    Fetch $cf "$Dir\Caddyfile"
    WriteLF "$Dir\Caddyfile" ((Get-Content "$Dir\Caddyfile" -Raw) -replace '\$\{DOMAIN\}', $HostAddr)
}
Info "已生成 docker-compose.yml"

# ---------- 5. 启动 ----------
Write-Host ""
if ($Build) { docker build -t expense-hub:local $SrcDir } else { docker compose pull }
docker compose up -d

Info "等待服务就绪…"
$healthUrl = if ($Mode -eq 'proxy') { "http://127.0.0.1/health" } else { "http://127.0.0.1:$Port/health" }
for ($i = 0; $i -lt 40; $i++) {
    try { Invoke-RestMethod $healthUrl -TimeoutSec 3 | Out-Null; break } catch { Start-Sleep 3 }
}

Write-Host ""
Write-Host "────────────────────────────────────────────"
Write-Host "  安装完成"
Write-Host "────────────────────────────────────────────"
Write-Host "  后台地址   $Url/admin"
Write-Host "  用户名     $AdminUser"
Write-Host "  密码       $AdminPassword"
Write-Host "────────────────────────────────────────────"
Write-Host "  日常管理： .\expense-hub.ps1 start|stop|info|update|backup"
if (-not $FeishuAppId) {
    Write-Host ""
    Write-Host "  提示：还没配飞书凭据，IM 通道未启用。补齐后重跑本命令即可。"
}
