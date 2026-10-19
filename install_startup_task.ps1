param(
    [string]$TaskName = "Roblox Discord Tracker Bot"
)

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Pyw = Join-Path $Root "run_bot.pyw"
$Pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source

if (-not $Pythonw) {
    Write-Error "pythonw.exe was not found. Install Python and make sure it is on PATH."
    exit 1
}

$Action = New-ScheduledTaskAction -Execute $Pythonw -Argument "`"$Pyw`"" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Runs the Roblox Discord tracker bot in a hidden Python window at logon." -Force | Out-Null
Write-Host "Installed startup task: $TaskName"
