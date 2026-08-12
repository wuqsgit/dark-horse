param(
    [Parameter(Mandatory = $true)]
    [string]$Root,

    [Parameter(Mandatory = $true)]
    [string]$Pattern,

    [string]$Ports = ""
)

$ErrorActionPreference = "SilentlyContinue"
$rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
$targetIds = [System.Collections.Generic.HashSet[int]]::new()

# All DarkHorse Python launchers live inside the workspace virtualenv. Stopping
# a launcher also terminates the child base-Python process without a WMI scan.
if ($Pattern -eq "__dark_horse_all__") {
    Get-Process | ForEach-Object {
        $path = $_.Path
        if (
            $_.Id -ne $PID -and
            $path -and
            $path.StartsWith(
                $rootPath,
                [System.StringComparison]::OrdinalIgnoreCase
            )
        ) {
            [void]$targetIds.Add([int]$_.Id)
        }
    }
}

$portSet = @(
    $Ports -split "," |
        Where-Object { $_ } |
        ForEach-Object { [int]$_ }
)
if ($portSet.Count -gt 0) {
    netstat.exe -ano -p tcp | ForEach-Object {
        $parts = @($_ -split '\s+' | Where-Object { $_ })
        if ($parts.Count -lt 5 -or $parts[3] -ne "LISTENING") {
            return
        }
        $localEndpoint = $parts[1]
        $portText = ($localEndpoint -split ':')[-1]
        if ($portText -match '^\d+$' -and $portSet -contains [int]$portText) {
            [void]$targetIds.Add([int]$parts[4])
        }
    }
}

foreach ($processId in $targetIds) {
    if ($processId -and $processId -ne $PID) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
}
