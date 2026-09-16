# 费用报销中枢 —— 宿主机 worker 一键安装（Windows）
#
#   irm https://raw.githubusercontent.com/PMJ520/ai-baoxiao-reimburse-bot/main/bridge/setup.ps1 -OutFile setup.ps1
#   .\setup.ps1 -Url http://localhost:8080 -Token XXXX
#
# 做四件事：检查依赖 → 认出你装的是哪个 CLI → 注册登录自启 → 起进程。

param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$Token,
    [string]$Cli,
    [switch]$NoAutostart
)

$ErrorActionPreference = 'Stop'
$Repo = if ($env:EH_REPO) { $env:EH_REPO } else { 'PMJ520/ai-baoxiao-reimburse-bot' }
$App  = Join-Path $env:USERPROFILE '.expense-hub'
$Url  = $Url.TrimEnd('/')

function Ok($m)   { Write-Host "  [OK] $m" }
function Warn($m) { Write-Host "  [!] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "`n[X] $m" -ForegroundColor Red; exit 1 }

Write-Host ''
Write-Host '费用报销中枢 · 宿主机 worker 安装'
Write-Host '────────────────────────────────'

# ---- 1. 环境检查 ----
Write-Host '检查运行环境'
if ($PSVersionTable.PSVersion.Major -lt 5) {
    Die "PowerShell 版本过低（$($PSVersionTable.PSVersion)），需要 5.1 及以上。Windows 10/11 自带满足要求。"
}
Ok "PowerShell $($PSVersionTable.PSVersion)"
Ok "系统：$((Get-CimInstance Win32_OperatingSystem).Caption)"

# ---- 2. 认出 CLI ----
if (-not $Cli) {
    foreach ($c in @('claude', 'codex')) {
        if (Get-Command $c -ErrorAction SilentlyContinue) { $Cli = $c; break }
    }
}
$cmd = if ($Cli) { Get-Command $Cli -ErrorAction SilentlyContinue } else { $null }
if (-not $cmd) {
    Die @"
没找到可用的 CLI。请先装好并登录其中之一：
     claude  ->  https://claude.com/claude-code
     codex   ->  https://github.com/openai/codex
   装完重新运行本脚本；也可以用 -Cli <命令名> 指定其它 CLI。
"@
}
$cliVer = (& $Cli --version 2>$null | Select-Object -First 1)
Ok "CLI：$Cli $cliVer（$($cmd.Source)）"

# ---- 3. 落地 worker 脚本 ----
New-Item -ItemType Directory -Force -Path $App | Out-Null
$workerPath = Join-Path $App 'worker.ps1'
$local = Join-Path $PSScriptRoot 'worker.ps1'
if (Test-Path $local) {
    Copy-Item $local $workerPath -Force
} else {
    try {
        Invoke-WebRequest "https://raw.githubusercontent.com/$Repo/main/bridge/worker.ps1" `
            -OutFile $workerPath -UseBasicParsing
    } catch { Die "下载 worker.ps1 失败：$($_.Exception.Message)" }
}
Ok "worker 脚本：$workerPath"

# 连通性预检：地址或令牌不对的话，现在就说清楚
try {
    $r = Invoke-WebRequest "$Url/bridge/jobs/next?wait=0" -Headers @{ Authorization = "Bearer $Token" } `
        -TimeoutSec 10 -UseBasicParsing
    Ok '后台连通，令牌有效'
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    switch ($code) {
        204     { Ok '后台连通，令牌有效' }
        401     { Die '令牌不正确。请到后台「系统设置 → 对话模型」核对桥接令牌。' }
        503     { Die '后台尚未设置桥接令牌。请先在后台保存令牌，再运行本脚本。' }
        default {
            if (-not $code) { Die "连不上 $Url 。worker 需要能访问后台地址；跨机器安装时别用 localhost。" }
            Warn "后台返回 HTTP $code，先继续装，装完请到后台点「测试连通性」确认"
        }
    }
}

# ---- 4. 登录自启 ----
$taskName = 'ExpenseHubWorker'
if (-not $NoAutostart) {
    Write-Host '配置登录自启'
    # 令牌写进任务的启动参数会出现在任务计划程序界面里，改用用户级环境变量
    [Environment]::SetEnvironmentVariable('EH_URL',   $Url,   'User')
    [Environment]::SetEnvironmentVariable('EH_TOKEN', $Token, 'User')
    [Environment]::SetEnvironmentVariable('EH_CLI',   $Cli,   'User')

    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$workerPath`""
    $trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    try {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Settings $settings -Description 'Expense Hub CLI worker' | Out-Null
        Start-ScheduledTask -TaskName $taskName
        Ok "已装为登录自启（计划任务：$taskName）"
    } catch {
        Warn "注册计划任务失败：$($_.Exception.Message)"
        Warn '可改为手动启动（见下方命令）'
        $NoAutostart = $true
    }
}

if ($NoAutostart) {
    Write-Host ''
    Write-Host '未配置自启，请自行运行：'
    Write-Host "  powershell -NoProfile -File `"$workerPath`" -Url $Url -Token <令牌> -Cli $Cli"
}

Write-Host ''
Write-Host '[OK] 安装完成' -ForegroundColor Green
Write-Host '   worker 已在后台运行，去后台「系统设置 → 对话模型」点「测试连通性」验证。'
if (-not $NoAutostart) {
    Write-Host "   查看状态：Get-ScheduledTask -TaskName $taskName"
    Write-Host "   停止它：  Unregister-ScheduledTask -TaskName $taskName -Confirm:`$false"
}
Write-Host ''
