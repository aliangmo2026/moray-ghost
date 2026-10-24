# moray-ghost 启动脚本：首次运行自动创建 .venv 并安装依赖，然后启动服务（127.0.0.1:8030）
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "[start] .venv not found; creating virtualenv..."
    $pyCandidates = @(
        "D:\python3.12\python.exe",
        (Get-Command python -ErrorAction SilentlyContinue).Source
    )
    $basePython = $pyCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    if (-not $basePython) { throw "no usable base python found (need 3.10+)" }
    Write-Host "[start] using base python: $basePython"
    & $basePython -m venv .venv
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r requirements.txt
}

Write-Host "[start] launching moray-ghost on 127.0.0.1:8030 ..."
& $venvPython run.py
