# Installs, repairs, uninstalls or starts DevDash on Windows, from this DevDash-BE clone (the
# folder above scripts\) and a DevDash-FE clone next to it. Double-click devdash.bat to run it.
# DEVDASH_FE set to a folder points it at a page clone somewhere else.
#
# Installing never touches your projects. Uninstalling deletes DevDash's data, after you
# type DELETE to confirm. (ASCII only: Windows PowerShell 5 misreads other characters.)

# Programs report failure by exit code, checked after each one that matters (Check); "Stop"
# would also stop on the warnings they print, under Windows PowerShell 5.
$ErrorActionPreference = "Continue"
$BE = Split-Path $PSScriptRoot -Parent
$FE = if ($env:DEVDASH_FE) { $env:DEVDASH_FE } else { Join-Path (Split-Path $BE -Parent) "DevDash-FE" }
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
    if (-not (Test-Path (Join-Path $BE "pyproject.toml"))) { Fail "This script belongs in DevDash-BE\scripts; there's no DevDash-BE in $BE." }
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
        Fail "No DevDash-FE folder at $FE, and no page built into DevDash-BE."
    }
}

# git, and GitHub's gh tool, which Sign in with GitHub on the dashboard uses. gh is optional:
# without it the dashboard asks for a pasted token instead. gh older than 2.17 can't sign in
# from the dashboard, so an old one is upgraded.
function Test-GhOld {
    $line = (gh --version 2>$null | Select-Object -First 1)
    if ($line -notmatch "gh version (\d+)\.(\d+)") { return $true }
    $major = [int]$Matches[1]; $minor = [int]$Matches[2]
    return ($major -lt 2) -or ($major -eq 2 -and $minor -lt 17)
}

function Assert-GitTools {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Install-WithWinget "Git" "Git.Git" }
    $gh = Get-Command gh -ErrorAction SilentlyContinue
    if ($gh -and -not (Test-GhOld)) { return }
    if ($gh) { Say "Your gh is too old to sign in from the dashboard ($(gh --version | Select-Object -First 1))." }
    else { Say "GitHub's gh tool isn't installed. Sign in with GitHub on the dashboard uses it." }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "Install it from https://cli.github.com, or connect GitHub on the dashboard by pasting a token."
        return
    }
    $verb = if ($gh) { "upgrade" } else { "install" }
    Write-Host "This runs: winget $verb -e --id GitHub.cli"
    if (-not (YesNo "Do it now?" "y")) { Write-Host "Skipped. You can still connect GitHub on the dashboard by pasting a token."; return }
    winget $verb -e --id GitHub.cli --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { Write-Host "winget couldn't $verb gh; connect GitHub on the dashboard by pasting a token instead." }
    Update-Path
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
    Assert-GitTools
    Build-Page
    Say "Installing DevDash..."
    Clear-Build
    Pipx install $BE
    Check "Installing DevDash"
    Save-Stamp
    After-Install
}

# The newest change to the code DevDash is installed from: DevDash-BE's code (not its built
# page, which is made from DevDash-FE) and DevDash-FE's. Start reinstalls when it moves on.
$Stamp = Join-Path $Data "installed-from"
function Get-SourceStamp {
    $files = @()
    foreach ($root in @((Join-Path $BE "src"), (Join-Path $BE "pyproject.toml"), (Join-Path $FE "src"),
                        (Join-Path $FE "index.html"), (Join-Path $FE "package.json"))) {
        if (Test-Path $root) {
            $files += Get-ChildItem -LiteralPath $root -Recurse -File -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -notmatch '[\\/](web|__pycache__|node_modules)[\\/]' }
        }
    }
    if (-not $files) { return "0" }
    $newest = ($files | Measure-Object -Property LastWriteTimeUtc -Maximum).Maximum
    return [string]([DateTimeOffset]$newest).ToUnixTimeSeconds()
}

# pipx builds from DevDash-BE; setuptools' build folder keeps files deleted since, so
# it's cleared first. Then the stamp notes what this install was made from.
function Save-Stamp {
    New-Item -ItemType Directory -Force -Path $Data | Out-Null
    Set-Content -LiteralPath $Stamp -Value (Get-SourceStamp)
}

function Clear-Build {
    $build = Join-Path $BE "build"
    if (Test-Path $build) { Remove-Item -Recurse -Force -LiteralPath $build }
}

# Start runs the installed copy, so edits to the code show only after reinstalling it.
function Update-IfChanged {
    if ((Test-Path $Stamp) -and ((Get-Content -LiteralPath $Stamp -Raw).Trim() -eq (Get-SourceStamp))) { return }
    Say "DevDash's code changed since it was installed. Updating..."
    Build-Page
    Stop-Running
    Clear-Build
    Pipx install --force $BE
    Check "Updating DevDash"
    Save-Stamp
}

# Reinstalls over whatever is there, broken or not. Your data stays.
function Repair-DevDash {
    Assert-Ready
    Assert-GitTools
    Build-Page
    Say "Reinstalling DevDash..."
    Stop-Running
    Clear-Build
    Pipx install --force $BE
    Check "Reinstalling DevDash"
    Save-Stamp
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
    Update-IfChanged
    $exe = Get-Command devdash -ErrorAction SilentlyContinue
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
