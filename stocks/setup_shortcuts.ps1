<#
.SYNOPSIS
    One-time setup: put double-clickable shortcuts for this project on the
    Desktop and in the Start Menu.

.DESCRIPTION
    Run this once and you never need a terminal again.

    The shortcuts do NOT rely on the .bat file association. Their target is
    cmd.exe, called explicitly with the script path as an argument:

        cmd.exe /c "C:\...\stock_view\launch.bat"

    A file association only decides what happens when Windows is asked to
    "open" a file - double-clicking it, mostly. Naming the interpreter
    outright skips that decision, which is why these shortcuts still work on a
    machine where double-clicking a .bat opens Notepad, or does nothing at all.
    That is the usual reason a .bat "will not run" when the command inside it
    is perfectly fine.

    Nothing is installed and nothing outside your Desktop and Start Menu is
    touched. -Remove takes the shortcuts away again.

.PARAMETER Remove
    Delete the shortcuts instead of creating them.

.PARAMETER NoStartMenu
    Only put shortcuts on the Desktop.

.PARAMETER ScanArguments
    Extra arguments baked into the scan shortcut, e.g. '--include-canada'.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File setup_shortcuts.ps1

    The -ExecutionPolicy Bypass matters: Windows refuses to run unsigned .ps1
    files by default, and it refuses quietly enough to look like a broken
    script. It applies to this one invocation only and changes no setting.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File setup_shortcuts.ps1 -ScanArguments '--include-canada'
#>

[CmdletBinding()]
param(
    [switch] $Remove,
    [switch] $NoStartMenu,
    [string] $ScanArguments = ''
)

$ErrorActionPreference = 'Stop'

function Write-Step  { param($m) Write-Host "  $m" }
function Write-Ok    { param($m) Write-Host "  [ok] $m"   -ForegroundColor Green }
function Write-Warn  { param($m) Write-Host "  [!]  $m"   -ForegroundColor Yellow }
function Write-Fail  { param($m) Write-Host "  [x]  $m"   -ForegroundColor Red }

# ---------------------------------------------------------------- locations --
$root      = if ($PSScriptRoot) { $PSScriptRoot }
             else { Split-Path -Parent $MyInvocation.MyCommand.Definition }
$viewBat   = Join-Path $root 'stock_view\launch.bat'
$scanBat   = Join-Path $root 'run_scan.bat'
$viewIcon  = Join-Path $root 'stock_view.ico'
$scanIcon  = Join-Path $root 'run_scan.ico'
$cmdExe    = Join-Path $env:SystemRoot 'System32\cmd.exe'

$desktop   = [Environment]::GetFolderPath('Desktop')
$startMenu = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\Stocks'

Write-Host ''
Write-Host '  Stocks - shortcut setup' -ForegroundColor Cyan
Write-Host '  ----------------------------------------------------------------'
Write-Step "project folder:  $root"
Write-Host ''

# ------------------------------------------------------------------ remove --
if ($Remove) {
    $targets = @(
        (Join-Path $desktop 'Stock View.lnk'),
        (Join-Path $desktop 'Run Stock Scan.lnk')
    )
    foreach ($path in $targets) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force
            Write-Ok "removed $path"
        }
    }
    if (Test-Path -LiteralPath $startMenu) {
        Remove-Item -LiteralPath $startMenu -Recurse -Force
        Write-Ok "removed $startMenu"
    }
    Write-Host ''
    Write-Host '  Done. Nothing else was changed.'
    Write-Host ''
    return
}

# ------------------------------------------------------------ sanity check --
$missing = @()
if (-not (Test-Path -LiteralPath $viewBat)) { $missing += $viewBat }
if (-not (Test-Path -LiteralPath $scanBat)) { $missing += $scanBat }
if ($missing.Count -gt 0) {
    Write-Fail 'Could not find the scripts these shortcuts point at:'
    $missing | ForEach-Object { Write-Host "        $_" }
    Write-Host ''
    Write-Host '  Put setup_shortcuts.ps1 in the folder that holds market_data.py'
    Write-Host '  and stock_view\, then run it again.'
    Write-Host ''
    exit 1
}

# ------------------------------------------------- diagnose .bat, for info --
# Worth reporting rather than fixing. Repointing the .bat association means
# writing to HKEY_CLASSES_ROOT, which is a system-wide change well outside what
# a shortcut installer should be doing on its own - and the shortcuts below
# make it unnecessary anyway.
try {
    $batOpen = (Get-ItemProperty -LiteralPath 'Registry::HKEY_CLASSES_ROOT\batfile\shell\open\command' `
                                 -Name '(default)' -ErrorAction Stop).'(default)'
    if ($batOpen -match '(?i)"?%1"?\s*%\*?' -and $batOpen -notmatch '(?i)notepad|code|editor|wordpad') {
        Write-Ok '.bat association looks normal on this machine'
    } else {
        Write-Warn ".bat files are opened by:  $batOpen"
        Write-Step '     That is why double-clicking launch.bat does not start it.'
        Write-Step '     The shortcuts below do not go through that association,'
        Write-Step '     so they work regardless.'
    }
} catch {
    Write-Warn 'could not read the .bat association (not a problem - the shortcuts do not use it)'
}
Write-Host ''

# ----------------------------------------------------------------- create --
$shell = New-Object -ComObject WScript.Shell

function New-AppShortcut {
    param(
        [string] $Path,
        [string] $Batch,
        [string] $Icon,
        [string] $Description,
        [string] $ExtraArguments = ''
    )

    $link = $shell.CreateShortcut($Path)
    $link.TargetPath = $cmdExe

    # The doubled outer quotes are cmd.exe's own rule: with /c, it strips one
    # pair, so a path containing spaces (C:\Users\...\My Stocks\) needs the
    # inner pair to survive that. Getting this wrong is the classic way a
    # shortcut works for one person and not for the next.
    $inner = '"' + $Batch + '"'
    if ($ExtraArguments) { $inner += ' ' + $ExtraArguments }
    $link.Arguments = '/c "' + $inner + '"'

    $link.WorkingDirectory = Split-Path -Parent $Batch
    $link.Description      = $Description
    if (Test-Path -LiteralPath $Icon) { $link.IconLocation = $Icon }
    $link.WindowStyle = 1
    $link.Save()
}

$made = @()
$places = @($desktop)
if (-not $NoStartMenu) {
    if (-not (Test-Path -LiteralPath $startMenu)) {
        New-Item -ItemType Directory -Path $startMenu -Force | Out-Null
    }
    $places += $startMenu
}

foreach ($place in $places) {
    $viewLink = Join-Path $place 'Stock View.lnk'
    New-AppShortcut -Path $viewLink -Batch $viewBat -Icon $viewIcon `
                    -Description 'Open the stock_view dashboard (reads the last scan; runs nothing)'
    $made += $viewLink

    $scanLink = Join-Path $place 'Run Stock Scan.lnk'
    New-AppShortcut -Path $scanLink -Batch $scanBat -Icon $scanIcon `
                    -Description 'Run the scan pipeline and print the Tier 1 report' `
                    -ExtraArguments $ScanArguments
    $made += $scanLink
}

foreach ($path in $made) { Write-Ok "created $path" }

Write-Host ''
Write-Host '  Done. Two icons are on your Desktop:' -ForegroundColor Cyan
Write-Host ''
Write-Host '    Stock View       opens the dashboard in your browser.'
Write-Host '                     Reads what is on disk. Never runs the scan.'
Write-Host ''
Write-Host '    Run Stock Scan   refreshes the data. This is the slow one that'
Write-Host '                     goes to the network. Run it, then press Reload'
Write-Host '                     in the dashboard sidebar.'
Write-Host ''
Write-Host '  You can drag either one to your taskbar to pin it.'
Write-Host '  Re-run with -Remove to take them away again.'
Write-Host ''
