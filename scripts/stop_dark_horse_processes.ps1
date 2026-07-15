param(
    [Parameter(Mandatory = $true)]
    [string]$Root,

    [Parameter(Mandatory = $true)]
    [string]$Pattern,

    [int[]]$Ports = @()
)

$ErrorActionPreference = "SilentlyContinue"
$rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
$allProcesses = @(Get-CimInstance Win32_Process)
$matches = @(
    $allProcesses | Where-Object {
        $_.ProcessId -ne $PID -and
        $_.CommandLine -and
        $_.CommandLine.Contains($Pattern) -and
        (
            ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) -or
            $_.CommandLine.Contains($rootPath)
        )
    }
)

$matchedIds = @($matches | ForEach-Object { [int]$_.ProcessId })
$roots = @($matches | Where-Object { $matchedIds -notcontains [int]$_.ParentProcessId })
foreach ($process in $roots) {
    & taskkill.exe /PID $process.ProcessId /T /F 2>$null | Out-Null
}

foreach ($port in $Ports) {
    $owners = @(
        Get-NetTCPConnection -State Listen -LocalPort $port |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
    foreach ($ownerPid in $owners) {
        if ($ownerPid -and $ownerPid -ne $PID) {
            & taskkill.exe /PID $ownerPid /T /F 2>$null | Out-Null
        }
    }
}
