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
$RunStatePath = Join-Path $RootDir ".dev-ports.json"
$ConfiguredBackendPort = $BackendPort
$ConfiguredFrontendPort = $FrontendPort
$BackendUrl = $null
$FrontendUrl = $null

$script:BackendProcess = $null
$script:FrontendProcess = $null
$script:Stopping = $false
$script:CancelHandler = $null
$script:PortCleanupEnabled = $false

function Set-ServerUrls {
    $script:BackendUrl = "http://127.0.0.1:$BackendPort/"
    $script:FrontendUrl = "http://127.0.0.1:$FrontendPort/"
}

Set-ServerUrls

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

    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "npm $($Arguments -join ' ') failed with exit code $exitCode."
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

function Get-PortListenerProcessIds {
    param([int]$Port)

    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        return @(
            Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique
        )
    }

    $rows = & netstat -ano -p tcp | Select-String -Pattern "LISTENING"
    return @(
        $rows |
            ForEach-Object {
                $line = $_.Line.Trim()
                if ($line -match "[:\.]$Port\s+.*LISTENING\s+(\d+)$") {
                    [int]$Matches[1]
                }
            } |
            Select-Object -Unique
    )
}

function Clear-Port {
    param(
        [int]$Port,
        [string]$Name,
        [string]$Reason
    )

    $processIds = @(Get-PortListenerProcessIds -Port $Port | Where-Object { $_ -and $_ -ne $PID })
    if (-not $processIds.Count) {
        Write-Info "Port $Port is clear for $Name."
        return
    }

    foreach ($processId in $processIds) {
        try {
            $process = Get-Process -Id $processId -ErrorAction Stop
            Write-Info "Clearing port $Port for $Name ($Reason): stopping PID $processId ($($process.ProcessName))."
            Stop-Process -Id $processId -Force -ErrorAction Stop
            Start-Sleep -Milliseconds 300
        }
        catch {
            Write-Warning "Could not clear port $Port for PID ${processId}: $($_.Exception.Message)"
        }
    }
}

function Get-RecordedDevelopmentPorts {
    if (-not (Test-Path -LiteralPath $RunStatePath)) {
        return @()
    }

    try {
        $state = Get-Content -LiteralPath $RunStatePath -Raw | ConvertFrom-Json
        $ports = @()
        foreach ($propertyName in @("backendPort", "frontendPort")) {
            if ($state.PSObject.Properties.Name -contains $propertyName) {
                $port = [int]$state.$propertyName
                if ($port -gt 0) {
                    $ports += [pscustomobject]@{
                        Port = $port
                        Name = "recorded $propertyName"
                    }
                }
            }
        }
        return $ports
    }
    catch {
        Write-Warning "Could not read recorded development ports: $($_.Exception.Message)"
        return @()
    }
}

function Clear-DevelopmentPorts {
    param([string]$Reason)

    $portSpecs = @(
        [pscustomobject]@{ Port = $ConfiguredBackendPort; Name = "configured backend" },
        [pscustomobject]@{ Port = $ConfiguredFrontendPort; Name = "configured frontend" },
        [pscustomobject]@{ Port = $BackendPort; Name = "backend" },
        [pscustomobject]@{ Port = $FrontendPort; Name = "frontend" }
    )
    $portSpecs += @(Get-RecordedDevelopmentPorts)

    $seenPorts = @{}
    foreach ($spec in $portSpecs) {
        if (-not $spec.Port -or $seenPorts.ContainsKey($spec.Port)) {
            continue
        }

        $seenPorts[$spec.Port] = $true
        Clear-Port -Port $spec.Port -Name $spec.Name -Reason $Reason
    }
}

function Save-DevelopmentPorts {
    $state = [ordered]@{
        backendPort = $BackendPort
        frontendPort = $FrontendPort
        backendUrl = $BackendUrl
        frontendUrl = $FrontendUrl
        updatedAt = (Get-Date).ToString("o")
    }

    $state | ConvertTo-Json | Set-Content -LiteralPath $RunStatePath -Encoding UTF8
}

function Clear-DevelopmentPortState {
    Remove-Item -LiteralPath $RunStatePath -ErrorAction SilentlyContinue
}

function Remove-FrontendPathWithRetry {
    param(
        [string]$Path,
        [string]$Description
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    $frontendFullPath = [System.IO.Path]::GetFullPath($FrontendDir).TrimEnd('\')
    $targetFullPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $targetFullPath.StartsWith($frontendFullPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove $Description outside frontend directory: $targetFullPath"
    }

    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Write-Info "Removing $Description at $targetFullPath"
            Remove-Item -LiteralPath $targetFullPath -Recurse -Force -ErrorAction Stop
            return
        }
        catch {
            if ($attempt -eq 3) {
                throw
            }

            Write-Warning "Could not remove $Description yet: $($_.Exception.Message). Retrying..."
            Start-Sleep -Seconds 1
        }
    }
}

function Test-PortListening {
    param([int]$Port)

    return @(Get-PortListenerProcessIds -Port $Port).Count -gt 0
}

function Find-AvailablePort {
    param(
        [int]$StartingPort,
        [string]$Name
    )

    for ($candidate = $StartingPort; $candidate -lt ($StartingPort + 100); $candidate++) {
        if (-not (Test-PortListening -Port $candidate) -and -not (Test-PortInUse -Port $candidate)) {
            return $candidate
        }
    }

    throw "Could not find an available $Name port starting at $StartingPort."
}

function Resolve-UsablePort {
    param(
        [int]$Port,
        [string]$Name
    )

    if (-not (Test-PortListening -Port $Port) -and -not (Test-PortInUse -Port $Port)) {
        return $Port
    }

    $fallbackPort = Find-AvailablePort -StartingPort ($Port + 1) -Name $Name
    Write-Warning "Configured $Name port $Port is still busy after cleanup; using $fallbackPort for this run."
    return $fallbackPort
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
            $installArgs = @("ci")
        }
        else {
            $installArgs = @("install")
        }

        try {
            Invoke-Npm $installArgs
        }
        catch {
            Write-Warning "Frontend dependency install failed: $($_.Exception.Message)"
            Write-Warning "Clearing frontend build cache and retrying once."
            Remove-FrontendPathWithRetry -Path (Join-Path $FrontendDir "node_modules\.cache") -Description "frontend build cache"

            try {
                Invoke-Npm $installArgs
            }
            catch {
                Write-Warning "Frontend dependency retry failed: $($_.Exception.Message)"
                Write-Warning "Removing frontend node_modules and retrying a clean install."
                Remove-FrontendPathWithRetry -Path (Join-Path $FrontendDir "node_modules") -Description "frontend node_modules"
                Invoke-Npm $installArgs
            }
        }

        $reactScriptsCommand = Join-Path $FrontendDir "node_modules\.bin\react-scripts.cmd"
        if ($env:OS -ne "Windows_NT") {
            $reactScriptsCommand = Join-Path $FrontendDir "node_modules/.bin/react-scripts"
        }

        if (-not (Test-Path -LiteralPath $reactScriptsCommand)) {
            Write-Warning "react-scripts was not installed; retrying frontend dependency install."
            Remove-FrontendPathWithRetry -Path (Join-Path $FrontendDir "node_modules") -Description "frontend node_modules"
            Invoke-Npm $installArgs
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
    if (-not $InstallOnly) {
        Write-Step "Clearing configured development ports before dependency install"
        $script:PortCleanupEnabled = $true
        Clear-DevelopmentPorts -Reason "preinstall"
    }

    Install-BackendDependencies
    Install-FrontendDependencies

    if ($InstallOnly) {
        Write-Step "All dependencies are installed"
        exit 0
    }

    Write-Step "Clearing configured development ports"
    Clear-DevelopmentPorts -Reason "startup"
    $BackendPort = Resolve-UsablePort -Port $BackendPort -Name "backend"
    $FrontendPort = Resolve-UsablePort -Port $FrontendPort -Name "frontend"
    Set-ServerUrls
    Save-DevelopmentPorts

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
    $frontendStartCommand = "set PORT=$FrontendPort&& set REACT_APP_API_BASE=http://127.0.0.1:$BackendPort&& npm.cmd start"
    $script:FrontendProcess = Start-ManagedProcess `
        -Name "frontend" `
        -FileName "cmd.exe" `
        -Arguments @("/d", "/s", "/c", $frontendStartCommand) `
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
    if ($script:PortCleanupEnabled) {
        Clear-DevelopmentPorts -Reason "shutdown"
        Clear-DevelopmentPortState
    }
    if ($script:CancelHandler) {
        [Console]::remove_CancelKeyPress($script:CancelHandler)
    }
    Get-EventSubscriber | Where-Object { $_.SourceObject -is [System.Diagnostics.Process] } | Unregister-Event
}
