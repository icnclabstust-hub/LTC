# 本機測試用：開 Cloudflare 免費通道（Quick Tunnel），並自動把 LINE Webhook 網址更新成通道網址。
#
# 用法（先啟動前端 port 8010）：在 frontend\tools 按兩下 start_cloudflare_tunnel.cmd
# 前置：winget install --id Cloudflare.cloudflared；frontend\.env 已填 LINE_CHANNEL_ACCESS_TOKEN
#
# Quick Tunnel 不用帳號與網域，但每次啟動網址都會變，所以每次都要重設 Webhook（本腳本自動處理）。
# LIFF 的 Endpoint URL 無法自動更新，網址變了要到 LINE Developers Console 手動改（或改用 ngrok 固定網域）。
# Cloudflare 標明 Quick Tunnel 僅供測試、不保證可用性；正式使用請改用具名通道或固定主機。

param([int]$Port = 8010)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$frontend = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $frontend '.env'
$log = Join-Path $env:TEMP 'xiaozhao-cloudflared.log'

if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    throw '找不到 cloudflared，請先執行：winget install --id Cloudflare.cloudflared（安裝後重開視窗）'
}
if (-not (Test-Path $envFile)) { throw "找不到 $envFile，請先由 .env.example 複製並填入 LINE 金鑰" }

$token = $null
foreach ($line in Get-Content $envFile -Encoding UTF8) {
    if ($line -match '^\s*LINE_CHANNEL_ACCESS_TOKEN\s*=\s*(.+?)\s*$') { $token = $Matches[1] }
}
if (-not $token) { throw 'frontend\.env 的 LINE_CHANNEL_ACCESS_TOKEN 是空的' }

try {
    Invoke-WebRequest "http://127.0.0.1:$Port/liff/privacy.html" -UseBasicParsing -TimeoutSec 5 | Out-Null
} catch {
    throw "前端沒有在 port $Port 執行，請先啟動前端再執行本腳本"
}

Remove-Item $log -ErrorAction SilentlyContinue
$startArgs = @{
    FilePath = 'cloudflared'
    ArgumentList = @('tunnel', '--no-autoupdate', '--url', "http://127.0.0.1:$Port")
    RedirectStandardError = $log
    PassThru = $true
}
# 只有 Windows 支援隱藏視窗（$IsWindows 在 Windows PowerShell 5.1 不存在，故另判斷 Desktop 版）
if ($PSVersionTable.PSEdition -eq 'Desktop' -or $IsWindows) { $startArgs.WindowStyle = 'Hidden' }
$tunnel = Start-Process @startArgs

try {
    $url = $null
    for ($i = 0; $i -lt 60 -and -not $url; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Path $log) {
            $found = Select-String -Path $log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' | Select-Object -First 1
            if ($found) { $url = $found.Matches[0].Value }
        }
        if ($tunnel.HasExited) { throw "cloudflared 已結束，請看紀錄：$log" }
    }
    if (-not $url) { throw "60 秒內沒有取得通道網址，請看紀錄：$log" }
    Write-Host "通道網址：$url"

    $headers = @{ Authorization = "Bearer $token" }
    $body = @{ endpoint = "$url/callback" } | ConvertTo-Json
    try {
        Invoke-RestMethod -Method Put -Uri 'https://api.line.me/v2/bot/channel/webhook/endpoint' -Headers $headers `
            -ContentType 'application/json; charset=utf-8' -Body $body | Out-Null
    } catch {
        throw "更新 LINE Webhook 失敗（$($_.Exception.Message)）。401 表示 LINE_CHANNEL_ACCESS_TOKEN 錯誤或已失效；也可手動到 LINE Developers Console 把 Webhook URL 設為 $url/callback"
    }
    Write-Host "已把 LINE Webhook 設為 $url/callback"

    # 新網址的 DNS 生效要幾秒，測試失敗時重試
    $ok = $false
    for ($i = 0; $i -lt 6 -and -not $ok; $i++) {
        Start-Sleep -Seconds 5
        $test = Invoke-RestMethod -Method Post -Uri 'https://api.line.me/v2/bot/channel/webhook/test' -Headers $headers `
            -ContentType 'application/json; charset=utf-8' -Body '{}'
        $ok = $test.success
        if (-not $ok) { Write-Host "Webhook 測試尚未成功（$($test.statusCode) $($test.reason)），重試中…" }
    }
    if ($ok) { Write-Host 'Webhook 測試成功，LINE 已接通。' -ForegroundColor Green }
    else { Write-Host 'Webhook 測試仍失敗：請確認 LINE Developers Console 的「Use webhook」已開啟、前端有在跑。' -ForegroundColor Yellow }

    Write-Host ''
    Write-Host "隱私權政策網址：$url/liff/privacy.html"
    Write-Host "LIFF Endpoint URL（網址變了要到 LINE Login channel 手動改）：$url/liff/"
    Write-Host ''
    Write-Host '通道執行中；按 Ctrl+C 結束（結束後 LINE 就收不到訊息）。'
    Wait-Process -Id $tunnel.Id
} finally {
    if (-not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -Force }
}
