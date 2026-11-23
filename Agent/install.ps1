# One-line installer for the Corpus2GeoRAG Pacific app, Windows (spec R105). In PowerShell or Command Prompt:
#
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/EtzionR/Corpus2GeoRAG_Pacific/main/Agent/install.ps1 | iex"
#
# The outer `powershell -ExecutionPolicy Bypass` runs it in a separate PowerShell with the script policy
# lifted for that one run, so a computer whose policy blocks scripts needs no setting changed.
#
# Everything goes into one folder (default: Corpus2GeoRAG_Pacific in your user folder): uv, Python 3.13,
# the packages, the code and your key. Nothing is installed system-wide and no admin rights are needed;
# deleting the folder removes it all. Running it again updates the app and keeps your key.
# Needs Git for Windows. Settings for testing: GEORAG_DIR, GEORAG_REPO, GEORAG_BRANCH, GEORAG_NO_START=1.
# Written for Windows PowerShell 5.1. It never calls `exit`: under `iex` that would close your window.

& {
    $ErrorActionPreference = 'Continue'   # native tools report failure by exit code; each one is checked
    $Repo   = if ($env:GEORAG_REPO)   { $env:GEORAG_REPO }   else { 'https://github.com/EtzionR/Corpus2GeoRAG_Pacific.git' }
    $Branch = if ($env:GEORAG_BRANCH) { $env:GEORAG_BRANCH } else { 'main' }
    $Dir    = if ($env:GEORAG_DIR)    { $env:GEORAG_DIR }    else { Join-Path $HOME 'Corpus2GeoRAG_Pacific' }
    $Code     = Join-Path $Dir 'code'
    $GraphDir = Join-Path $Code 'Agent\data\partof521'
    $Graph    = Join-Path $GraphDir 'graph.selffix.local.json'
    $EnvFile  = Join-Path $Code 'Agent\.env'
    $Uv       = Join-Path $Dir '.uv\uv.exe'

    function Say([string]$Text) { Write-Host ''; Write-Host $Text -ForegroundColor Cyan }
    function Write-NoBom([string]$Path, [string]$Text) {
        # Windows PowerShell's Set-Content adds a byte-order mark, which would break the first line of .env.
        [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding $false))
    }

    try {
        # --- Git -----------------------------------------------------------------------------
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
            # Just installed in another window? This window doesn't see it yet: look in the usual place.
            $gitCmd = Join-Path $env:ProgramFiles 'Git\cmd'
            if (Test-Path (Join-Path $gitCmd 'git.exe')) { $env:Path = "$gitCmd;$env:Path" }
        }
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
            throw 'Git is not installed. Install "Git for Windows" from https://git-scm.com/download/win (keep the default options), then run this command again.'
        }

        New-Item -ItemType Directory -Force -Path $Dir | Out-Null
        # uv and the Python it downloads live inside the folder, not in your user profile.
        $env:UV_UNMANAGED_INSTALL  = Join-Path $Dir '.uv'
        $env:UV_PYTHON_INSTALL_DIR = Join-Path $Dir '.uv\python'
        $env:UV_CACHE_DIR          = Join-Path $Dir '.uv\cache'
        $env:UV_PYTHON_PREFERENCE  = 'only-managed'

        # --- 1. uv -----------------------------------------------------------------------------
        Say '1/5  Installing uv (the tool that installs Python and the packages)'
        if (-not (Test-Path $Uv)) {
            # A separate PowerShell, so uv's installer can't affect this window.
            powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex'
            if (-not (Test-Path $Uv)) { throw 'Could not download uv. Check your internet connection.' }
        }

        # --- 2. The code: only App\ and Agent\, without Git LFS files ----------------------
        Say '2/5  Downloading the app'
        $env:GIT_LFS_SKIP_SMUDGE = '1'
        if (Test-Path (Join-Path $Code '.git')) {
            git -C $Code remote set-url origin $Repo   # always update from the repository asked for now
            git -C $Code fetch -q --depth 1 origin $Branch
            if ($LASTEXITCODE -ne 0) { throw "Could not download the update (branch $Branch of $Repo). Check your internet connection." }
            # --keep updates the files but stops, without losing anything, if you edited a file the update changes.
            git -C $Code reset -q --keep FETCH_HEAD
            if ($LASTEXITCODE -ne 0) { throw "You changed files in $Code that the update also changes. Undo your changes, or delete that folder, and run again." }
        } else {
            git clone -q --depth 1 --filter=blob:none --sparse --branch $Branch $Repo $Code
            if ($LASTEXITCODE -ne 0) { throw 'Could not download the app. Check your internet connection.' }
            git -C $Code sparse-checkout set App Agent
            if ($LASTEXITCODE -ne 0) { throw "Could not download the app's folders." }
        }

        # --- 3. The graph: unpack the .gz and check it against SHA256SUMS -------------------
        Say '3/5  Unpacking the knowledge graph'
        if (-not (Test-Path (Join-Path $GraphDir 'graph.selffix.json.gz'))) {
            throw "The downloaded code has no compressed graph (Agent\data\partof521\graph.selffix.json.gz on branch $Branch of $Repo). That version of the app predates the installer: ask the agent developer."
        }
        $want = $null
        foreach ($line in Get-Content (Join-Path $GraphDir 'SHA256SUMS')) {
            $parts = $line -split '\s+'
            if ($parts.Count -ge 2 -and $parts[1] -eq 'graph.selffix.json') { $want = $parts[0] }
        }
        if (-not $want) { throw "The graph's checksum is missing from $GraphDir\SHA256SUMS." }
        $have = if (Test-Path $Graph) { (Get-FileHash -Algorithm SHA256 $Graph).Hash } else { '' }
        if ($have -ne $want) {   # -ne ignores case: Get-FileHash gives upper case
            $tmp = "$Graph.tmp"
            $in = [IO.File]::OpenRead((Join-Path $GraphDir 'graph.selffix.json.gz'))
            try {
                $gz = New-Object IO.Compression.GZipStream($in, [IO.Compression.CompressionMode]::Decompress)
                $out = [IO.File]::Create($tmp)
                try { $gz.CopyTo($out) } finally { $out.Close(); $gz.Close() }
            } finally { $in.Close() }
            if ((Get-FileHash -Algorithm SHA256 $tmp).Hash -ne $want) {
                Remove-Item $tmp -Force
                throw 'The unpacked graph is damaged (checksum mismatch). Run again.'
            }
            Move-Item $tmp $Graph -Force
        }

        # --- 4. Python 3.13 and the packages ---------------------------------------------------
        Say "4/5  Installing Python 3.13 and the app's packages (the first time takes a few minutes)"
        $Venv = Join-Path $Dir '.venv'
        & $Uv venv -q --allow-existing --python 3.13 $Venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not install Python 3.13.' }
        & $Uv pip install -q --python $Venv --only-binary cryptography -r (Join-Path $Code 'App\requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw "Could not install the app's packages. Check your internet connection." }
        & $Uv cache prune -q --ci 2>$null   # the downloaded packages are installed now; free the space

        # --- 5. The OpenRouter key: asked once, saved only in Agent\.env -------------------
        Say '5/5  Your OpenRouter API key'
        $key = ''
        if (Test-Path $EnvFile) {
            foreach ($line in Get-Content $EnvFile) {
                if ($line -like 'OPENROUTER_API_KEY=*') { $key = $line.Substring('OPENROUTER_API_KEY='.Length); break }
            }
        }
        $prompt = if ($key) { 'A key is already saved. Press Enter to keep it, or paste a new one' } else { 'Paste your key (it stays hidden; press Enter to skip and use demo mode)' }
        $secure = Read-Host -Prompt $prompt -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $typed = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        $typed = ($typed -replace '\s', '')
        if ($typed) { $key = $typed }
        if (-not $key) { Write-Host 'No key for now: the app runs in demo mode, with recorded answers. The launcher asks for a key again each time you start the app.' }
        elseif ($key -like 'sk-or-*') { Write-Host 'Key saved.' }
        else { Write-Host 'Key saved, but it does not look like an OpenRouter key (they start with sk-or-).' }
        $graphFwd = $Graph -replace '\\', '/'
        Write-NoBom $EnvFile ("# Written by install.ps1. Keep this file private: whoever has the key can spend its credit.`n" +
                              "OPENROUTER_API_KEY=$key`nGRAPH_PATH=$graphFwd`nGEO_AGENT_RUN_TIMEOUT=240`n")

        # --- The launcher, its key prompt, and a desktop shortcut -------------------------------
        # set-key.ps1: when no key is saved (it was skipped during the install), start.bat asks again,
        # hidden; Enter starts demo mode this time. A batch file can't read hidden input itself.
        $setKey = @'
# Asks for the OpenRouter key when none is saved (written by install.ps1, run by start.bat).
if ($env:GEORAG_SKIP_KEY_PROMPT -eq '1') { return }   # right after the installer, which just asked
$envFile = Join-Path $PSScriptRoot 'code\Agent\.env'
$lines = @()
if (Test-Path $envFile) { $lines = @(Get-Content $envFile) }
foreach ($l in $lines) { if ($l -match '^OPENROUTER_API_KEY=.+') { return } }
Write-Host 'No OpenRouter key is saved, so the app would run in demo mode (recorded answers only).'
$s = Read-Host -Prompt 'Paste your key to use the real agent (it stays hidden), or press Enter for demo mode' -AsSecureString
$b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
try { $k = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
$k = $k -replace '\s', ''
if (-not $k) { return }
$out = @(); $done = $false
foreach ($l in $lines) {
    if ($l -like 'OPENROUTER_API_KEY=*') { $out += "OPENROUTER_API_KEY=$k"; $done = $true } else { $out += $l }
}
if (-not $done) { $out += "OPENROUTER_API_KEY=$k" }
[IO.File]::WriteAllText($envFile, (($out -join "`n") + "`n"), (New-Object Text.UTF8Encoding $false))
Write-Host 'Key saved.'
'@
        Write-NoBom (Join-Path $Dir 'set-key.ps1') (($setKey -split "`r?`n") -join "`r`n")
        $bat = @(
            '@echo off',
            'rem Starts the Corpus2GeoRAG Pacific app (written by install.ps1). Close this window to stop it.',
            'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0set-key.ps1"',
            'cd /d "%~dp0code\App"',
            'echo Starting the app at http://localhost:8501 (the first start takes up to a minute). Keep this window open while you use it.',
            'start "" /b powershell -NoProfile -WindowStyle Hidden -Command "for($i=0;$i -lt 180;$i++){try{$null=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://localhost:8501/_stcore/health;Start-Process http://localhost:8501;break}catch{Start-Sleep 1}}"',
            '"%~dp0.venv\Scripts\streamlit.exe" run app.py --server.address localhost --server.port 8501 --server.headless true --browser.gatherUsageStats false',
            'if errorlevel 1 pause'
        ) -join "`r`n"
        Write-NoBom (Join-Path $Dir 'start.bat') ($bat + "`r`n")
        try {
            $desktop = [Environment]::GetFolderPath('Desktop')
            $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop 'Start GeoRAG.lnk'))
            $shortcut.TargetPath = Join-Path $Dir 'start.bat'
            $shortcut.WorkingDirectory = $Dir
            $shortcut.Save()
            $made = 'a "Start GeoRAG" shortcut on your desktop'
        } catch {
            $made = "start.bat in $Dir"
        }

        Say "Done. The app is installed in $Dir"
        Write-Host "Next time, double-click $made."
        if ($env:GEORAG_NO_START -ne '1') {
            $env:GEORAG_SKIP_KEY_PROMPT = '1'   # the key was just asked for
            try { & (Join-Path $Dir 'start.bat') } finally { Remove-Item Env:GEORAG_SKIP_KEY_PROMPT -ErrorAction SilentlyContinue }
        }
    } catch {
        Write-Host ''
        Write-Host "Stopped: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host 'Fix the problem above and run the same command again.'
    }
}
