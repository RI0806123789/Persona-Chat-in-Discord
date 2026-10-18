# Persona Bot Console を、追加のターミナルを開かずに起動する。
# 専用のブラウザ画面を閉じると、バックエンドと Vite も終了する。
# 接続先は backend\profiles.json に設定する。詳しくは README を参照。

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$python = Join-Path $root "backend\.venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "backend\.venv がありません。README の「初回セットアップ」を先に実行してください。" -ForegroundColor Yellow
    exit 1
}

# 仮想環境の転送用 exe を挟まず、監視役の実プロセスを直接起動する。
# 監視役は標準ライブラリだけを使い、バックエンドには仮想環境を使う。
$basePython = & $python -B -c 'import sys; print(sys._base_executable)'
if ($LASTEXITCODE -ne 0 -or -not $basePython) {
    throw "Python を起動できません。仮想環境を確認してください。"
}
$pythonw = Join-Path (Split-Path -Parent $basePython) "pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonw)) {
    throw "Python の非表示起動用プログラムが見つかりません。Python のインストールを確認してください。"
}

$run = Join-Path $root ".run"
[void][System.IO.Directory]::CreateDirectory($run)
$statusFile = Join-Path $run ("startup-" + [guid]::NewGuid().ToString("N") + ".json")
$launcher = Join-Path $root "dev_launcher.py"
# Start-Process は引数を連結するため、空白を含むパスを明示的に引用する。
$worker = Start-Process -FilePath $pythonw -ArgumentList @(
    "-B", ('"' + $launcher + '"'), "--status-file", ('"' + $statusFile + '"')
) -WorkingDirectory $root -WindowStyle Hidden -PassThru

try {
    $deadline = [DateTime]::UtcNow.AddSeconds(55)
    while (-not (Test-Path -LiteralPath $statusFile)) {
        if ($worker.HasExited -and -not (Test-Path -LiteralPath $statusFile)) {
            throw "起動処理が終了しました。.run\launcher.log を確認してください。"
        }
        if ([DateTime]::UtcNow -ge $deadline) {
            # この起動で作った監視プロセスだけを停止する。子プロセスは Job が回収する。
            $worker.Kill()
            throw "起動確認がタイムアウトしました。.run 内のログを確認してください。"
        }
        Start-Sleep -Milliseconds 100
    }
    $status = Get-Content -LiteralPath $statusFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $status.ok) { throw $status.message }
    Write-Host $status.message
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Yellow
    exit 1
} finally {
    # この呼び出しで作った、一時的な起動結果だけを片付ける。
    if (Test-Path -LiteralPath $statusFile) {
        [System.IO.File]::Delete($statusFile)
    }
    $worker.Dispose()
}
