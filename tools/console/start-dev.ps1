# Persona Bot Console を開発モードで起動する（PowerShell）
#   1. バックエンド（127.0.0.1:8765）
#   2. 画面（Vite / 127.0.0.1:5173）
# をそれぞれ別ウィンドウで起動し、ブラウザで画面を開く。
# 接続先は backend\profiles.json（無ければ profiles.example.json）に書く。README の「本番サーバーにつなぐ」を参照。
#
# 使い方:  .\start-dev.ps1

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
if (-not (Test-Path (Join-Path $root "backend\profiles.json"))) {
    Write-Host "backend\profiles.json がありません。見本（profiles.example.json）を読むので、YOUR_ を書き換えるまで接続できません。" -ForegroundColor Yellow
}

Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$root\backend'; & '$python' app.py"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$root\frontend'; npm run dev"

Start-Sleep -Seconds 3
Start-Process "http://127.0.0.1:5173"
