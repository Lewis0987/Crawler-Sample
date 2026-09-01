# =====================================================================
# Phase 6.9 FIRST LIVE - one-time scheduled task
# =====================================================================
# Creates a SINGLE one-time task. It does NOT run anything now.
#
# The task boots the runner at 23:59 so that Python startup, module
# import, HMI login and meter subscription all finish BEFORE the
# absolute observation window opens at 00:00:00. The runner then waits
# for the wall-clock start and observes 00:00:00 - 00:10:00.
# Nothing is dispatched during bootstrap.
#
# If bootstrap finishes later than 00:00:00 the runner aborts with
# MISSED_OBSERVATION_WINDOW and sends no command.
# =====================================================================

$TaskName    = 'Phase6_FirstLive_Charge_OneTime'
$PythonExe   = 'C:\Users\Water\AppData\Local\Programs\Python\Python314\python.exe'
$WorkDir     = 'D:\Crawler Sample\test'
$ScriptName  = 'phase6_first_live_runner.py'
$WindowStart = '2026-09-01 00:00:00'
$BootAt      = Get-Date -Year 2026 -Month 8 -Day 31 -Hour 23 -Minute 59 -Second 0
$Arguments   = '{0} --window-start "{1}"' -f $ScriptName, $WindowStart

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed existing task: $TaskName"
}

$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument $Arguments -WorkingDirectory $WorkDir

$Trigger = New-ScheduledTaskTrigger -Once -At $BootAt

$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew

$Description = 'Phase 6.9 FIRST LIVE - ONE-TIME. Boots 23:59, observes absolute window 2026-09-01 00:00-00:10 read-only, then fresh precheck. Dispatches CHARGE 5.0kW only if every condition passes. Aborts on any failure.'

# Side-effect-free parameter-binding check first: build the definition in
# memory. If any parameter fails to bind, this throws before anything is
# registered. Syntax parsing alone does NOT catch binding errors.
$Definition = New-ScheduledTask `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description $Description

Write-Output "Task definition built OK (nothing registered yet)."

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description $Description `
    | Out-Null

$T = Get-ScheduledTask -TaskName $TaskName
$I = Get-ScheduledTaskInfo -TaskName $TaskName

Write-Output ''
Write-Output '=== Registered task ==='
Write-Output ("TaskName           : " + $T.TaskName)
Write-Output ("State              : " + $T.State)
Write-Output ("Execute            : " + $T.Actions[0].Execute)
Write-Output ("Arguments          : " + $T.Actions[0].Arguments)
Write-Output ("WorkingDirectory   : " + $T.Actions[0].WorkingDirectory)
Write-Output ("Trigger StartAt    : " + $T.Triggers[0].StartBoundary)
Write-Output ("Repetition         : " + $(if ($T.Triggers[0].Repetition.Interval) { $T.Triggers[0].Repetition.Interval } else { 'NONE (one-time)' }))
Write-Output ("WakeToRun          : " + $T.Settings.WakeToRun)
Write-Output ("StartWhenAvailable : " + $T.Settings.StartWhenAvailable)
Write-Output ("ExecutionTimeLimit : " + $T.Settings.ExecutionTimeLimit)
Write-Output ("MultipleInstances  : " + $T.Settings.MultipleInstances)
Write-Output ("NextRunTime        : " + $I.NextRunTime)
