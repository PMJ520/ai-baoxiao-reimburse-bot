# 费用报销中枢 · 管理脚本（Windows）
#   .\expense-hub.ps1 start|stop|restart|status|logs|info|update|backup|uninstall
param([Parameter(Position = 0)][string]$Cmd = "")
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Test-Path "creds.json")) { Write-Host "找不到配置，请先运行 install.ps1"; exit 1 }
$c = Get-Content "creds.json" -Raw | ConvertFrom-Json
# compose 从进程环境读变量，每次运行都要注入
$c.PSObject.Properties | ForEach-Object { Set-Item -Path "Env:$($_.Name)" -Value $_.Value }

function UrlBase {
    if (Test-Path "Caddyfile") {
        $d = (Get-Content "Caddyfile" -TotalCount 1).Split(' ')[0] -replace '^https?://', ''
        return "https://$d"
    }
    return "http://localhost:$($c.HOST_PORT)"
}
function Banner {
    Write-Host ""
    Write-Host "────────────────────────────────────────────"
    Write-Host "  后台地址   $(UrlBase)/admin"
    Write-Host "  用户名     $($c.ADMIN_USER)"
    Write-Host "  密码       $($c.ADMIN_PASSWORD)"
    Write-Host "────────────────────────────────────────────"
    Write-Host "  凭据同时保存在 $PSScriptRoot\creds.json"
    Write-Host ""
}
function WaitHealthy {
    $u = if (Test-Path "Caddyfile") { "http://127.0.0.1/health" }
         else { "http://127.0.0.1:$($c.HOST_PORT)/health" }
    for ($i = 0; $i -lt 40; $i++) {
        try { Invoke-RestMethod $u -TimeoutSec 3 | Out-Null; return $true } catch { Start-Sleep 3 }
    }
    return $false
}

switch ($Cmd) {
    "start" {
        docker compose up -d
        if (WaitHealthy) { Write-Host "✅ 服务已启动"; Banner }
        else { Write-Host "⚠️  启动超时，用 .\expense-hub.ps1 logs 查看原因"; exit 1 } }
    "stop"    { docker compose down; Write-Host "已停止" }
    "restart" { docker compose restart; if (WaitHealthy) { Write-Host "✅ 已重启"; Banner } }
    "status"  { docker compose ps }
    "logs"    { docker compose logs -f --tail=200 }
    "info"    { Banner }
    "update"  { docker compose pull; docker compose up -d
                if (WaitHealthy) { Write-Host "✅ 已更新到最新版本" } }
    "backup"  {
        $ts = Get-Date -Format "yyyyMMdd-HHmmss"
        $out = "backup-$ts.zip"
        Compress-Archive -Path "$($c.DATA_DIR)\*" -DestinationPath $out -Force
        Write-Host "✅ 已备份到 $PSScriptRoot\$out" }
    "uninstall" {
        $a = Read-Host "将停止并删除容器（数据保留在 $($c.DATA_DIR)）。继续？[y/N]"
        if ($a -match '^[yY]') { docker compose down
            Write-Host "已卸载。数据仍在 $($c.DATA_DIR)。" } }
    default { Write-Host "用法: .\expense-hub.ps1 {start|stop|restart|status|logs|info|update|backup|uninstall}" }
}
