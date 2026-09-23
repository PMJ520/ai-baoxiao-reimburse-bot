# 费用报销中台 —— 宿主机 worker（Windows PowerShell 5.1+，系统自带）
#
#   .\worker.ps1 -Url http://localhost:8080 -Token XXXX [-Cli claude]
#
# 只依赖 PowerShell 和你那个 CLI 本身。

param(
    [string]$Url   = $env:EH_URL,
    [string]$Token = $env:EH_TOKEN,
    [string]$Cli   = $env:EH_CLI
)

$ErrorActionPreference = 'Stop'
if (-not $Url)   { Write-Error '缺少 -Url';   exit 2 }
if (-not $Token) { Write-Error '缺少 -Token'; exit 2 }
$Url = $Url.TrimEnd('/')

if (-not $Cli) {
    foreach ($c in @('claude', 'codex')) {
        if (Get-Command $c -ErrorAction SilentlyContinue) { $Cli = $c; break }
    }
}
if (-not (Get-Command $Cli -ErrorAction SilentlyContinue)) {
    Write-Error "找不到 CLI：$(if ($Cli) { $Cli } else { 'claude/codex 都没装' })"; exit 3
}

# 版本号要拼进 URL 的 query，先洗掉空格括号之类（claude 会输出 "2.1.270 (Claude Code)"）
function Clean([string]$v) {
    if (-not $v) { return '' }
    $v = ($v -split '\s+')[0]
    ($v -replace '[^A-Za-z0-9._-]', '').Substring(0, [Math]::Min(24, ($v -replace '[^A-Za-z0-9._-]', '').Length))
}
$ver  = Clean (& $Cli --version 2>$null | Select-Object -First 1)
$arch = Clean $env:PROCESSOR_ARCHITECTURE
$auth = @{ Authorization = "Bearer $Token" }
# PowerShell 5.1 默认按本地编码收发，中文提示词会乱码，这里强制 UTF-8
$utf8 = New-Object System.Text.UTF8Encoding($false)

Write-Host "worker 已启动：$Cli $ver @ Windows $arch → $Url"

function Invoke-Cli([string]$prompt) {
    switch ($Cli) {
        'claude' { & claude -p $prompt 2>&1 }
        'codex'  { & codex exec $prompt 2>&1 }
        default  { & $Cli $prompt 2>&1 }
    }
}

$fails = 0
while ($true) {
    $q = "$Url/bridge/jobs/next?wait=30&os=Windows&arch=$arch&cli=$Cli&ver=$ver"
    try {
        $r = Invoke-WebRequest -Uri $q -Headers $auth -TimeoutSec 45 -UseBasicParsing
        $fails = 0
    } catch {
        $code = $_.Exception.Response.StatusCode.value__
        if ($code -eq 401 -or $code -eq 503) {
            Write-Warning "鉴权失败（HTTP $code）：令牌不对或后台还没配置桥接令牌"
            Start-Sleep -Seconds 10; continue
        }
        $fails++
        if ($fails -eq 1) { Write-Warning "连不上后台：$($_.Exception.Message)" }
        Start-Sleep -Seconds ([Math]::Min($fails * 5, 30)); continue
    }

    if ($r.StatusCode -eq 204) { continue }          # 没活，接着等
    $id = $r.Headers['X-Job-Id']
    if (-not $id) { Write-Warning '响应缺少 X-Job-Id，跳过'; continue }

    # 走原始字节流自己按 UTF-8 解码：PowerShell 5.1 对 text/* 响应的字符集
    # 判断不可靠，直接用 .Content 可能拿到已被错误编码解过一遍的中文
    $prompt = $utf8.GetString($r.RawContentStream.ToArray())

    $failed = $false
    try {
        $out = (Invoke-Cli $prompt | Out-String)
        if ($LASTEXITCODE -ne 0) { $failed = $true }
    } catch {
        $out = $_.Exception.Message; $failed = $true
    }

    $target = "$Url/bridge/jobs/$id/result"
    if ($failed) {
        Write-Warning "CLI 执行失败（任务 $id）"
        $target += '?error=1'
    }
    try {
        Invoke-WebRequest -Uri $target -Method Post -Headers $auth -TimeoutSec 30 `
            -ContentType 'text/plain; charset=utf-8' `
            -Body $utf8.GetBytes($out) -UseBasicParsing | Out-Null
    } catch {
        Write-Warning "回传结果失败：$($_.Exception.Message)"
    }
}
