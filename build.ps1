$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

$iconPath = Join-Path $projectRoot "assets\app_icon.ico"
$versionPath = Join-Path $projectRoot "version_info.txt"
if (-not (Test-Path -LiteralPath $iconPath)) {
    throw "Missing application icon: $iconPath"
}
if (-not (Test-Path -LiteralPath $versionPath)) {
    throw "Missing version resource: $versionPath"
}

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --noupx `
    --name "CodexDesktopAccountManager" `
    --icon $iconPath `
    --version-file $versionPath `
    --add-data "assets;assets" `
    --collect-all customtkinter `
    --hidden-import atomic_io `
    --hidden-import manager `
    --hidden-import oauth_login `
    --hidden-import recover_from_backups `
    --hidden-import token_usage `
    --hidden-import usage `
    --hidden-import windows_app `
    main.py

$output = Join-Path $projectRoot "dist\CodexDesktopAccountManager.exe"
if (-not (Test-Path -LiteralPath $output)) {
    throw "Build finished without the expected executable: $output"
}

$item = Get-Item -LiteralPath $output
Write-Host "Built: $($item.FullName)"
Write-Host "Size : $([math]::Round($item.Length / 1MB, 2)) MB"
