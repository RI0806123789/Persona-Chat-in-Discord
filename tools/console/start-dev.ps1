# Persona Bot Console を開発モードで起動する（PowerShell）
#   1. 開発用の模擬 SSH サーバー（127.0.0.1:2222）
#   2. バックエンド（127.0.0.1:8765）
#   3. 画面（Vite / 127.0.0.1:5173）
# をそれぞれ別ウィンドウで起動し、ブラウザで画面を開く。
# 本番サーバーに接続するだけなら 1 は不要（-NoMock を付ける）。
#
# 使い方:  .\start-dev.ps1          （模擬サーバーも起動）
#          .\start-dev.ps1 -NoMock  （本番など実サーバーだけを使う）

param([switch]$NoMock)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root "backend\.venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "backend\.venv がありません。README の「初回セットアップ」を先に実行してください。" -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path (Join-Path $root "frontend\node_modules"))) {
    Write-Host "frontend\node_modules がありません。README の「初回セットアップ」を先に実行してください。" -ForegroundColor Yellow
    exit 1
}

if (-not $NoMock) {
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$root\backend'; & '$python' dev_ssh_server.py"
}
Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$root\backend'; & '$python' app.py"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$root\frontend'; npm run dev"

Start-Sleep -Seconds 3
Start-Process "http://127.0.0.1:5173"
