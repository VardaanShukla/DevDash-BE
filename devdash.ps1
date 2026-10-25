# Installs, repairs, uninstalls or starts DevDash on Windows, from the DevDash-BE and
# DevDash-FE folders next to this Scripts folder. Double-click devdash.bat to run it.
#
# Installing never touches your projects. Uninstalling deletes DevDash's data, after you
# type DELETE to confirm. (ASCII only: Windows PowerShell 5 misreads other characters.)

# Programs report failure by exit code, checked after each one that matters (Check); "Stop"
# would also stop on the warnings they print, under Windows PowerShell 5.
$ErrorActionPreference = "Continue"
$Root = Split-Path $PSScriptRoot -Parent
$BE = Join-Path $Root "DevDash-BE"
$FE = Join-Path $Root "DevDash-FE"
$Data = if ($env:DEVDASH_HOME) { $env:DEVDASH_HOME } else { Join-Path $HOME ".devdash" }
$Url = "http://127.0.0.1:8765"
$Bin = Join-Path $HOME ".local\bin"  # where pipx puts devdash.exe

function Say($text) { Write-Host "`n$text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`n$text" -ForegroundColor Red; exit 1 }
function YesNo($question, $default) {
    $hint = if ($default -eq "y") { "[Y/n]" } else { "[y/N]" }
    $answer = Read-Host "$question $hint"
    if (-not $answer) { $answer = $default }
    return $answer -match "^[Yy]"
}

# What winget or pipx just installed is on PATH for new windows only: read it again here.
function Update-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
        [Environment]::GetEnvironmentVariable("Path", "User") + ";$Bin"
}
Update-Path

# Python 3.10 or newer, through the py launcher or python. The "python" that only opens the
# Microsoft Store fails the version check, so it isn't picked.
function Find-Python {
    foreach ($candidate in @(@{ Exe = "py"; Args = @("-3") }, @{ Exe = "python"; Args = @() })) {
        if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
        $args_ = $candidate.Args + @("-c", "import sys; sys.exit(sys.version_info < (3, 10))")
        & $candidate.Exe @args_ 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    return $null
}
function Py {
    $all = $script:Python.Args + $args
    & $script:Python.Exe @all
}
function Pipx { Py -m pipx @args }
function Check($what) { if ($LASTEXITCODE -ne 0) { Fail "$what failed (exit code $LASTEXITCODE)." } }

function Test-Installed {
    if (-not $script:Python) { return $false }
    $list = Pipx list --short 2>$null
    return $LASTEXITCODE -eq 0 -and ($list -match "^devdash ")
}

# Installs a program with winget, after asking. Windows may ask you to allow it.
function Install-WithWinget($what, $id) {
    Say "$what isn't installed."
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail "Install $what yourself (winget isn't on this computer), then run this again."
    }
    Write-Host "This runs: winget install -e --id $id"
    Write-Host "If Windows asks to allow changes, that prompt is Windows' own; this script never sees your password."
    if (-not (YesNo "Install it now?" "y")) { Fail "Install $what, then run this again." }
    winget install -e --id $id --accept-package-agreements --accept-source-agreements
    Check "Installing $what"
    Update-Path
}

function Assert-Ready {
    if (-not (Test-Path $BE)) { Fail "No DevDash-BE folder next to Scripts (looked in $Root)." }
    if (-not $script:Python) {
        Install-WithWinget "Python 3.10 or newer" "Python.Python.3.12"
        $script:Python = Find-Python
        if (-not $script:Python) { Fail "Python is installed but this window can't find it yet. Close it and run devdash.bat again." }
    }
    Pipx --version 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Say "Installing pipx, which installs DevDash in a place of its own..."
        Py -m pip install --user pipx
        Check "Installing pipx"
        Pipx ensurepath | Out-Null
    }
    # The page is built from DevDash-FE when it's here; without it, the page already built
    # into DevDash-BE is used, and Node isn't needed.
    if (Test-Path $FE) {
        if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { Install-WithWinget "Node.js" "OpenJS.NodeJS.LTS" }
        $major = [int](node -p "process.versions.node.split('.')[0]")
        if ($major -lt 18) { Fail "Building the page needs Node 18 or newer; this computer has $(node --version). Get a newer one from https://nodejs.org." }
    } elseif (-not (Test-Path (Join-Path $BE "src\devdash\web\index.html"))) {
        Fail "No DevDash-FE folder next to Scripts, and no page built into DevDash-BE."
    }
}

function Build-Page {
    if (-not (Test-Path $FE)) { return }
    Say "Building the page from DevDash-FE..."
    Py (Join-Path $BE "scripts\build_ui.py") $FE
    Check "Building the page"
}

function Stop-Running {
    $running = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*devdash*serve*" }
    if (-not $running) { return }
    Write-Host "Stopping the DevDash that's running..."
    $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
}

function After-Install {
    Say "DevDash is installed. Your data is in $Data."
    $default = Join-Path $HOME "Projects"
    $folder = Read-Host "`nWhere are your projects? DevDash lists the git repos in that folder. [$default, or Enter to skip]"
    if (-not $folder -and (Test-Path $default)) { $folder = $default }
    if ($folder) {
        if (Test-Path $folder -PathType Container) { devdash init $folder }
        else { Write-Host "There's no folder $folder. Add projects later under Add project on the dashboard." }
    }
    if (YesNo "`nStart DevDash now?" "y") { Start-DevDash }
}

function Install-DevDash {
    if (Test-Installed) { Say "DevDash is already installed. Choose Repair to reinstall it."; return }
    Assert-Ready
    Build-Page
    Say "Installing DevDash..."
    Pipx install $BE
    Check "Installing DevDash"
    After-Install
}

# Reinstalls over whatever is there, broken or not. Your data stays.
function Repair-DevDash {
    Assert-Ready
    Build-Page
    Say "Reinstalling DevDash..."
    Stop-Running
    Pipx install --force $BE
    Check "Reinstalling DevDash"
    After-Install
}

function Uninstall-DevDash {
    Say "Uninstall DevDash and delete its data?"
    Write-Host "This deletes:"
    Write-Host "  - the devdash program"
    Write-Host "  - everything in ${Data}: your DevDash account, the project list, recorded AI history and explanations"
    Write-Host "  - DevDash's hooks in your projects and in Claude Code, Codex and Antigravity"
    Write-Host "It doesn't touch your projects' files or git history, Ollama, or its models."
    $answer = Read-Host "`nType DELETE to continue, or press Enter to cancel"
    if ($answer -cne "DELETE") { Write-Host "Cancelled. Nothing was changed."; return }

    Stop-Running
    if (Get-Command devdash -ErrorAction SilentlyContinue) {
        devdash remove-hooks
        if ($LASTEXITCODE -ne 0) { Write-Host "Couldn't take every hook out; carrying on." }
    }
    if (Test-Installed) { Pipx uninstall devdash; Check "Uninstalling DevDash" }
    # Never anything but DevDash's own folder.
    if ($Data -and ($Data -ne $HOME) -and (Test-Path $Data)) { Remove-Item -Recurse -Force -LiteralPath $Data }
    Say "DevDash is uninstalled and its data deleted."
}

function Start-DevDash {
    $exe = Get-Command devdash -ErrorAction SilentlyContinue
    if (-not $exe) { Fail "DevDash isn't installed. Choose Install first." }
    if (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*devdash*serve*" }) {
        Write-Host "DevDash is already running."
        Start-Process $Url
        return
    }
    $server = Start-Process -FilePath $exe.Source -ArgumentList "serve" -NoNewWindow -PassThru
    Start-Sleep -Seconds 2
    Start-Process $Url
    Write-Host "Opened $Url. Keep this window open while you use DevDash; close it or press Ctrl+C to stop."
    Wait-Process -Id $server.Id
}

$Python = Find-Python
Say "DevDash"
if (Test-Installed) { Write-Host "Installed. Data in $Data." } else { Write-Host "Not installed yet." }
Write-Host @"

  1  Install      first time on this computer
  2  Repair       reinstall over the current one; your data stays
  3  Uninstall    remove DevDash and delete its data
  4  Start        run DevDash and open it in the browser
  5  Quit
"@
switch (Read-Host "`nChoose 1-5") {
    "1" { Install-DevDash }
    "2" { Repair-DevDash }
    "3" { Uninstall-DevDash }
    "4" { Start-DevDash }
    default { Write-Host "Nothing changed." }
}
