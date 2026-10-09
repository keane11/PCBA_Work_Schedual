param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Install', 'Remove')]
    [string]$Mode
)

$ErrorActionPreference = 'Stop'
$taskName = 'PCBA-Workbench-Boot'
$serviceBat = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\service.bat')).Path
$projectRoot = Split-Path -Parent $serviceBat
$arguments = '/d /c ""' + $serviceBat + '" run-bg"'
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw 'Run service.bat install-boot or uninstall-boot from an elevated Command Prompt.'
}

function Test-OwnedTask($scheduledTask) {
    $actions = @($scheduledTask.Actions)
    return ($actions.Count -eq 1 -and
            $actions[0].Execute -eq $env:ComSpec -and
            $actions[0].Argument -eq $arguments)
}

if ($Mode -eq 'Remove') {
    $existing = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
    if ($existing) {
        if (-not (Test-OwnedTask $existing)) {
            throw "The task named $taskName does not point to this workbench. Inspect Task Scheduler before removing it."
        }
        Unregister-ScheduledTask -TaskName $taskName -TaskPath '\' -Confirm:$false
        Write-Host "Removed boot task: $taskName"
    } else {
        Write-Host "Boot task was not installed: $taskName"
    }
    exit 0
}

# cmd.exe's /c requires two quote pairs when a batch path contains spaces.
$existing = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
if ($existing -and -not (Test-OwnedTask $existing)) {
    throw "A scheduled task with this name points to another command: $taskName. Inspect Task Scheduler before proceeding."
}
$action = New-ScheduledTaskAction -Execute $env:ComSpec -Argument $arguments -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -RestartCount 12 -RestartInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Installed boot task: $taskName (starts next boot as SYSTEM; up to 12 retries at five-minute intervals if startup fails)."
