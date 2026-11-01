# Double-click launcher for testers (via Start-App.bat): prepares .venv once, then serves the
# app to this computer only. Works on Windows PowerShell 5.1; touches no credentials.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $repoRoot
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
$versionCheck = 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'

function Test-Python([string]$Command, [string[]]$Arguments) {
    try {
        & $Command @Arguments -c $versionCheck 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Test-AppPortAvailable([int]$Port) {
    # Listener inventories can omit ports owned by another process or execution environment.
    # Test the addresses Streamlit will actually bind, and never probe a public interface.
    $listeners = @()
    try {
        foreach ($address in [Net.Dns]::GetHostAddresses('localhost')) {
            $listener = New-Object Net.Sockets.TcpListener($address, $Port)
            $listeners += $listener
            $listener.Server.ExclusiveAddressUse = $true
            $listener.Start()
        }
        return $true
    } catch [Net.Sockets.SocketException] {
        if ($_.Exception.SocketErrorCode -notin @([Net.Sockets.SocketError]::AddressAlreadyInUse,
                                                [Net.Sockets.SocketError]::AccessDenied)) { throw }
        return $false
    } finally {
        foreach ($listener in $listeners) { $listener.Stop() }
    }
}

function Get-AppPort([int]$StartPort = 8501) {
    for ($port = $StartPort; $port -le 65535; $port++) {
        if (Test-AppPortAvailable -Port $port) { return $port }
    }
    throw '找不到可用的本機連接埠，請關閉其他介面後再試。'
}

try {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        $candidates = @(@{Command = 'py'; Arguments = @('-3')}, @{Command = 'python'; Arguments = @()})
        $base = $candidates | Where-Object {
            (Get-Command $_.Command -ErrorAction SilentlyContinue) -and (Test-Python $_.Command $_.Arguments)
        } | Select-Object -First 1
        if (-not $base) {
            throw '找不到 Python 3.11 以上版本。請從 https://www.python.org/downloads/ 安裝（勾選 Add python.exe to PATH）後再執行一次。'
        }
        Write-Host '第一次啟動：建立 Python 虛擬環境（.venv）...'
        $baseArguments = $base.Arguments
        & $base.Command @baseArguments -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw '建立 .venv 失敗。' }
    }
    if (-not (Test-Python $venvPython @())) {
        throw '.venv 的 Python 版本低於 3.11。請刪除專案中的 .venv 資料夾後再執行一次。'
    }

    # Reinstall only when requirements.txt changes, so later launches start immediately.
    $marker = Join-Path $repoRoot '.venv\tronclass-requirements.sha256'
    $wanted = (Get-FileHash -LiteralPath (Join-Path $repoRoot 'requirements.txt') -Algorithm SHA256).Hash
    $installed = ''
    if (Test-Path -LiteralPath $marker) { $installed = (Get-Content -LiteralPath $marker -Raw).Trim() }
    if ($installed -ne $wanted) {
        # Streamlit ships files about 150 characters below .venv; Windows refuses paths over 260.
        $longPaths = Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -ErrorAction SilentlyContinue
        if ($repoRoot.Length -gt 80 -and -not ($longPaths -and $longPaths.LongPathsEnabled -eq 1)) {
            throw '專案資料夾的路徑太長，套件會因 Windows 路徑長度限制而安裝失敗。請把整個專案資料夾移到較短的位置（例如 C:\tronclass-mcp），刪除其中的 .venv 資料夾後再執行一次。'
        }
        Write-Host '安裝或更新套件（requirements.txt），第一次約需一兩分鐘...'
        & $venvPython -m pip install --disable-pip-version-check -r requirements.txt
        if ($LASTEXITCODE -ne 0) { throw '套件安裝失敗，請確認網路後再執行一次。' }
        Set-Content -LiteralPath $marker -Value $wanted -Encoding ascii
    }

    $port = Get-AppPort
    $url = "http://localhost:$port"
    # Headless skips Streamlit's first-run email prompt; localhost keeps the page off the network.
    $streamlitArguments = @('-m', 'streamlit', 'run', 'app.py', '--server.address', 'localhost',
        '--server.port', "$port", '--server.headless', 'true')
    $app = Start-Process -FilePath $venvPython -ArgumentList $streamlitArguments -WorkingDirectory $repoRoot -NoNewWindow -PassThru

    $ready = $false
    for ($attempt = 0; $attempt -lt 60 -and -not $app.HasExited; $attempt++) {
        $probe = New-Object Net.Sockets.TcpClient
        try {
            $probe.Connect('localhost', $port)
            $app.Refresh()
            if (-not $app.HasExited) { $ready = $true; break }
        }
        catch { Start-Sleep -Milliseconds 500 }
        finally { $probe.Close() }
    }
    $app.Refresh()
    if (-not $ready -or $app.HasExited) {
        if (-not $app.HasExited) { Stop-Process -Id $app.Id }
        throw '介面沒有成功啟動，請查看上方訊息。'
    }
    Write-Host ''
    Write-Host "介面已啟動：$url" -ForegroundColor Green
    Write-Host '使用時請保持這個視窗開啟；關閉視窗即停止介面。'
    Start-Process $url
    Wait-Process -Id $app.Id
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
