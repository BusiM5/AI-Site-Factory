[CmdletBinding()]
param(
    [switch]$InstallOnly,
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 3000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendDir = Join-Path $RootDir "backend"
$FrontendDir = Join-Path $RootDir "frontend"
$VenvDir = Join-Path $RootDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$BackendUrl = "http://127.0.0.1:$BackendPort/"
$FrontendUrl = "http://127.0.0.1:$FrontendPort/"

$script:BackendProcess = $null
$script:FrontendProcess = $null
$script:Stopping = $false
$script:CancelHandler = $null

function Write-Step {
    param([string]$Message)

    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Info {
    param([string]$Message)

    Write-Host "    $Message" -ForegroundColor DarkGray
}

function Assert-Command {
    param([string]$Name)

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found in PATH."
    }
}

function Invoke-Npm {
    param([string[]]$Arguments)

    if ($env:OS -eq "Windows_NT") {
        & npm.cmd @Arguments
    }
    else {
        & npm @Arguments
    }
}

function Test-PortInUse {
    param([int]$Port)

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connectTask = $client.ConnectAsync("127.0.0.1", $Port)
        if (-not $connectTask.Wait(300)) {
            return $false
        }

        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function New-VirtualEnvironment {
    if (Test-Path $VenvPython) {
        Write-Info "Using existing virtual environment at $VenvDir"
        return
    }

    Write-Step "Creating Python virtual environment"

    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv $VenvDir
    }
    else {
        Assert-Command "python"
        & python -m venv $VenvDir
    }
}

function Install-BackendDependencies {
    Write-Step "Installing backend dependencies"
    New-VirtualEnvironment

    & $VenvPython -m pip install --upgrade pip
    & $VenvPython -m pip install -r (Join-Path $BackendDir "requirements.txt")

    $DevRequirements = Join-Path $BackendDir "requirements-dev.txt"
    if (Test-Path $DevRequirements) {
        & $VenvPython -m pip install -r $DevRequirements
    }
}

function Install-FrontendDependencies {
    Write-Step "Installing frontend dependencies"
    Assert-Command "npm"

    Push-Location $FrontendDir
    try {
        if (Test-Path "package-lock.json") {
            Invoke-Npm @("ci")
        }
        else {
            Invoke-Npm @("install")
        }
    }
    finally {
        Pop-Location
    }
}

function Start-ManagedProcess {
    param(
        [string]$Name,
        [string]$FileName,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FileName
    $startInfo.Arguments = ($Arguments | ForEach-Object {
        if ($_ -match '\s|"' ) {
            '"' + ($_ -replace '"', '\"') + '"'
        }
        else {
            $_
        }
    }) -join " "
    if (-not $startInfo.Arguments) {
        $startInfo.Arguments = ""
    }
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    $process.EnableRaisingEvents = $true

    $outputAction = {
        if ($EventArgs.Data) {
            Write-Host "[$($Event.MessageData)] $($EventArgs.Data)"
        }
    }

    $errorAction = {
        if ($EventArgs.Data) {
            Write-Host "[$($Event.MessageData)] $($EventArgs.Data)" -ForegroundColor DarkYellow
        }
    }

    [void]$process.Start()
    $null = Register-ObjectEvent -InputObject $process -EventName OutputDataReceived -Action $outputAction -MessageData $Name
    $null = Register-ObjectEvent -InputObject $process -EventName ErrorDataReceived -Action $errorAction -MessageData $Name
    $process.BeginOutputReadLine()
    $process.BeginErrorReadLine()

    return $process
}

function Stop-ManagedProcess {
    param(
        [System.Diagnostics.Process]$Process,
        [string]$Name
    )

    if (-not $Process -or $Process.HasExited) {
        return
    }

    Write-Info "Stopping $Name..."
    try {
        if ($env:OS -eq "Windows_NT") {
            & taskkill /PID $Process.Id /T /F | Out-Null
        }
        else {
            $Process.Kill()
        }

        [void]$Process.WaitForExit(5000)
    }
    catch {
        Write-Warning "Could not stop $Name cleanly: $($_.Exception.Message)"
    }
}

function Stop-Servers {
    if ($script:Stopping) {
        return
    }

    $script:Stopping = $true
    Stop-ManagedProcess -Process $script:FrontendProcess -Name "frontend"
    Stop-ManagedProcess -Process $script:BackendProcess -Name "backend"
}

function Wait-ForBackend {
    Write-Step "Waiting for backend at $BackendUrl"

    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        if ($script:BackendProcess.HasExited) {
            throw "Backend exited before becoming ready. Exit code: $($script:BackendProcess.ExitCode)"
        }

        try {
            Invoke-RestMethod -Uri $BackendUrl -TimeoutSec 2 | Out-Null
            Write-Info "Backend is ready."
            return
        }
        catch {
            Start-Sleep -Milliseconds 700
        }
    }

    throw "Backend did not become ready within 45 seconds."
}

function Wait-ForFrontend {
    Write-Step "Waiting for frontend at $FrontendUrl"

    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline) {
        if ($script:FrontendProcess.HasExited) {
            throw "Frontend exited before becoming ready. Exit code: $($script:FrontendProcess.ExitCode)"
        }

        try {
            Invoke-WebRequest -Uri $FrontendUrl -UseBasicParsing -TimeoutSec 2 | Out-Null
            Write-Info "Frontend is ready."
            return
        }
        catch {
            Start-Sleep -Seconds 1
        }
    }

    throw "Frontend did not become ready within 120 seconds."
}

try {
    Install-BackendDependencies
    Install-FrontendDependencies

    if ($InstallOnly) {
        Write-Step "All dependencies are installed"
        exit 0
    }

    if (Test-PortInUse -Port $BackendPort) {
        throw "Port $BackendPort is already in use. Stop the process using it, then run this command again."
    }

    $script:CancelHandler = [System.ConsoleCancelEventHandler] {
        param($Source, $CancelEventArgs)
        $CancelEventArgs.Cancel = $true
        Stop-Servers
    }
    [Console]::add_CancelKeyPress($script:CancelHandler)

    Write-Step "Starting backend"
    $script:BackendProcess = Start-ManagedProcess `
        -Name "backend" `
        -FileName $VenvPython `
        -Arguments @("-m", "uvicorn", "main:app", "--reload", "--host", "127.0.0.1", "--port", "$BackendPort") `
        -WorkingDirectory $BackendDir

    Wait-ForBackend

    Write-Step "Starting frontend"
    Write-Info "Frontend will use http://127.0.0.1:$BackendPort as its API base by default."
    $script:FrontendProcess = Start-ManagedProcess `
        -Name "frontend" `
        -FileName "cmd.exe" `
        -Arguments @("/d", "/s", "/c", "npm.cmd start") `
        -WorkingDirectory $FrontendDir

    Wait-ForFrontend

    Write-Step "Development servers are running"
    Write-Info "Backend:  $BackendUrl"
    Write-Info "Frontend: http://localhost:$FrontendPort"
    Write-Info "Press Ctrl+C to stop both servers."

    while (-not $script:Stopping) {
        if ($script:BackendProcess.HasExited) {
            throw "Backend stopped unexpectedly. Exit code: $($script:BackendProcess.ExitCode)"
        }

        if ($script:FrontendProcess.HasExited) {
            $exitCode = $script:FrontendProcess.ExitCode
            Stop-Servers
            exit $exitCode
        }

        Start-Sleep -Seconds 1
    }
}
finally {
    Stop-Servers
    if ($script:CancelHandler) {
        [Console]::remove_CancelKeyPress($script:CancelHandler)
    }
    Get-EventSubscriber | Where-Object { $_.SourceObject -is [System.Diagnostics.Process] } | Unregister-Event
}
