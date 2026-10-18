@echo off
rem Run start_cloudflare_tunnel.ps1 in this folder, bypassing the PowerShell execution policy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_cloudflare_tunnel.ps1" %*
pause
