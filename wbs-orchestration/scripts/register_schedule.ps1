<#
.SYNOPSIS
Register one Windows scheduled task for the WBS skill: dispatch (wbs.py run) or report (render_wbs.py).

.DESCRIPTION
Each call registers exactly one task under a name you choose. Register the
dispatch job and the report job as two tasks with different names.
- Dispatch job: give -ConfigPath, -CliName and -WorkingDirectory.
- Report job: give -OutputPath.
An existing task with the same name is never replaced. The task runs as the
current user, only while that user is logged on, with limited rights. No
password is asked for or stored. This script uses only built-in cmdlets and
does not change the execution policy. Add -WhatIf to preview.

.EXAMPLE
.\register_schedule.ps1 -TaskName "WBS example report" -PythonExe C:\path\to\python.exe -DbPath C:\path\to\state.sqlite3 -OutputPath C:\path\to\wbs-status.html -IntervalMinutes 10 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}$')]
    [string]$TaskName,

    [ValidatePattern('^\\([A-Za-z0-9][A-Za-z0-9 ._-]*\\)*$')]
    [string]$TaskPath = '\',

    [Parameter(Mandatory)][string]$PythonExe,
    [Parameter(Mandatory)][string]$DbPath,
    [Parameter(Mandatory)][ValidateRange(1, 1440)][int]$IntervalMinutes,
    [ValidateRange(1, 1440)][int]$TimeLimitMinutes = 120,

    [Parameter(Mandatory, ParameterSetName = 'Run')][string]$ConfigPath,
    [Parameter(Mandatory, ParameterSetName = 'Run')]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')]
    [string]$CliName,
    [Parameter(Mandatory, ParameterSetName = 'Run')][string]$WorkingDirectory,

    [Parameter(Mandatory, ParameterSetName = 'Report')][string]$OutputPath
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

function Resolve-Existing([string]$Path, [string]$PathType, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType $PathType)) { throw "$Label not found: $Path" }
    (Resolve-Path -LiteralPath $Path).ProviderPath
}

# Arguments are resolved file paths (no '"', no trailing '\') or names limited to [A-Za-z0-9._-].
function Format-Arguments([string[]]$Items) {
    ($Items | ForEach-Object { '"' + $_ + '"' }) -join ' '
}

$python = Resolve-Existing $PythonExe Leaf 'Python executable'
$db = Resolve-Existing $DbPath Leaf 'Database (run wbs.py init first)'
if ($PSCmdlet.ParameterSetName -eq 'Run') {
    $pyScript = Resolve-Existing (Join-Path $PSScriptRoot 'wbs.py') Leaf 'wbs.py'
    $config = Resolve-Existing $ConfigPath Leaf 'Config'
    $workDir = Resolve-Existing $WorkingDirectory Container 'Working directory'
    $arguments = Format-Arguments @($pyScript, '--db', $db, 'run', '--config', $config, '--cli', $CliName)
} else {
    $pyScript = Resolve-Existing (Join-Path $PSScriptRoot 'render_wbs.py') Leaf 'render_wbs.py'
    $fullOutput = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputPath)
    $workDir = Resolve-Existing (Split-Path -Parent $fullOutput) Container 'Output folder'
    $arguments = Format-Arguments @($pyScript, '--db', $db, '--output', $fullOutput)
}

$existing = Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue |
    Where-Object { $_.TaskName -eq $TaskName }
if ($existing) { throw "Scheduled task '$TaskPath$TaskName' already exists; it is left unchanged. Choose another name." }

$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $workDir
# First start one minute from now, then every IntervalMinutes with no end date.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
# IgnoreNew: skip a start while the previous run of this task is still going.
# DontStopIfGoingOnBatteries: never cut a dispatch run off mid-CLI; starting on battery stays disallowed.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes $TimeLimitMinutes)
$job = $PSCmdlet.ParameterSetName.ToLowerInvariant()

if ($PSCmdlet.ShouldProcess("$TaskPath$TaskName", "Register $job task every $IntervalMinutes min: $python $arguments")) {
    # No -Force: Register-ScheduledTask itself also refuses to replace an existing task.
    Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Description "WBS $job job (register_schedule.ps1)" | Out-Null
    [pscustomobject]@{
        Task             = "$TaskPath$TaskName"
        Job              = $job
        Execute          = $python
        Arguments        = $arguments
        WorkingDirectory = $workDir
        IntervalMinutes  = $IntervalMinutes
    }
}
