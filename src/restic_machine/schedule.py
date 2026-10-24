# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""The backup schedule, as a task of the Windows Task Scheduler: backups run with the web page
closed and nobody logged in.

The task runs `restic-machine backup` as the user, with the highest privileges (VSS needs
them), logged on as "S4U": Windows keeps no password for it, so restic-machine never needs the
user's. Creating or changing such a task takes administrator rights, so it goes through
Windows' own UAC prompt; reading its state doesn't.
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import config

TASK_NAME = "restic-machine backup"
# New-ScheduledTaskTrigger writes the start with today's UTC offset (…T04:00:00+02:00), which ties
# the trigger to that offset: after a change of daylight saving time it would run an hour off.
# Without the offset the time is local, and 04:00 stays 04:00 all year.
_LOCAL_TIME = "$t.StartBoundary = $t.StartBoundary.Substring(0, 19)"
PACKAGE_DIR = Path(__file__).parent
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


class ScheduleError(Exception):
    pass


def supported() -> bool:
    return os.name == "nt"


def _powershell(script: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
                          stdin=subprocess.DEVNULL, **_NO_WINDOW)


def _quote(text: str) -> str:
    """A PowerShell single-quoted string."""
    return "'" + str(text).replace("'", "''") + "'"


def describe(schedule: dict) -> str:
    """The schedule in words, e.g. "every hour from 08:00 to 23:00, and at 04:00"."""
    parts = []
    if schedule["every_hours"]:
        every = "every hour" if schedule["every_hours"] == 1 else f"every {schedule['every_hours']} hours"
        parts.append(f"{every} from {schedule['from']} to {schedule['to']}")
    if schedule["also_at"]:
        parts.append("at " + ", ".join(schedule["also_at"]))
    return ", and ".join(parts) or "never"


def run_times(schedule: dict) -> list[str]:
    """Every time of day a backup starts."""
    times = set(schedule["also_at"])
    if schedule["every_hours"]:
        start, end = (int(t[:2]) * 60 + int(t[3:]) for t in (schedule["from"], schedule["to"]))
        times |= {f"{m // 60:02d}:{m % 60:02d}" for m in range(start, end + 1, schedule["every_hours"] * 60)}
    return sorted(times)


def check(schedule: dict) -> None:
    if schedule["every_hours"] and schedule["to"] <= schedule["from"]:
        raise ScheduleError("the repeated backups must end later in the day than they start")
    if not run_times(schedule):
        raise ScheduleError("the schedule has no backup at all")


def task_command(cfg: config.Config) -> tuple[str, str, str]:
    """(program, arguments, working folder) that run `restic-machine backup` with this config."""
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    return str(python), f'-B -m restic_machine --config "{cfg.path}" backup', str(PACKAGE_DIR.parent)


def install_script(cfg: config.Config, disable: list[str], result_file: Path) -> str:
    """The PowerShell that (elevated) registers our task and disables the ones given."""
    schedule = cfg.backup["schedule"]
    check(schedule)
    program, arguments, folder = task_command(cfg)
    triggers = []
    if schedule["every_hours"]:
        start = datetime.strptime(schedule["from"], "%H:%M")
        span = datetime.strptime(schedule["to"], "%H:%M") - start
        minutes = int(span.total_seconds() // 60)
        triggers.append(
            f"$t = New-ScheduledTaskTrigger -Daily -At {_quote(schedule['from'])}; "
            f"$t.Repetition = (New-ScheduledTaskTrigger -Once -At {_quote(schedule['from'])} "
            f"-RepetitionInterval (New-TimeSpan -Hours {schedule['every_hours']}) "
            f"-RepetitionDuration (New-TimeSpan -Minutes {minutes})).Repetition; {_LOCAL_TIME}; $triggers += $t")
    for at in schedule["also_at"]:
        triggers.append(f"$t = New-ScheduledTaskTrigger -Daily -At {_quote(at)}; {_LOCAL_TIME}; $triggers += $t")
    disables = "\n".join(f"    Disable-ScheduledTask -TaskName {_quote(name)} | Out-Null" for name in disable)
    return f"""$ErrorActionPreference = 'Stop'
$result = {_quote(result_file)}
try {{
    $triggers = @()
    {chr(10).join('    ' + t for t in triggers).lstrip()}
    $action = New-ScheduledTaskAction -Execute {_quote(program)} -Argument {_quote(arguments)} -WorkingDirectory {_quote(folder)}
    $principal = New-ScheduledTaskPrincipal -UserId {_quote(os.environ.get('USERNAME', ''))} -LogonType S4U -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 6)
    Register-ScheduledTask -TaskName {_quote(TASK_NAME)} -Description 'restic-machine: backups (restic-machine backup)' -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null
{disables}
    '{{"ok": true}}' | Set-Content -Encoding utf8 $result
}} catch {{
    @{{ ok = $false; error = "$_" }} | ConvertTo-Json -Compress | Set-Content -Encoding utf8 $result
}}
"""


def install(cfg: config.Config, disable: list[str] = ()) -> dict:
    """Register (or update) the task, after Windows' UAC prompt. Waits for the answer."""
    if not supported():
        raise ScheduleError("scheduling backups is only done on Windows so far")
    folder = config.default_data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    script, result = folder / "install-task.ps1", folder / "install-task.json"
    result.unlink(missing_ok=True)
    script.write_text(install_script(cfg, list(disable), result), encoding="utf-8-sig")
    try:
        launched = _powershell(
            "Start-Process -FilePath powershell.exe -Verb RunAs -Wait -WindowStyle Hidden "
            f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',{_quote(chr(34) + str(script) + chr(34))}",
            timeout=600)
        if not result.exists():
            cancelled = "cancel" in (launched.stderr or "").lower()
            return {"ok": False, "error": "cancelled at the administrator prompt" if cancelled
                    else f"the administrator step did not finish: {(launched.stderr or '').strip()[:300]}"}
        return json.loads(result.read_text(encoding="utf-8-sig"))
    finally:
        script.unlink(missing_ok=True)
        result.unlink(missing_ok=True)


_STATE = r"""
$ErrorActionPreference = 'SilentlyContinue'
$found = Get-ScheduledTask | Where-Object { (($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join ' ') -match 'restic' }
$out = foreach ($t in $found) {
    $i = Get-ScheduledTaskInfo -TaskName $t.TaskName -TaskPath $t.TaskPath
    [pscustomobject]@{
        name = $t.TaskName; state = [string]$t.State
        logon = [string]$t.Principal.LogonType; level = [string]$t.Principal.RunLevel
        action = (($t.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join '; ')
        triggers = @($t.Triggers | ForEach-Object { [pscustomobject]@{ start = [string]$_.StartBoundary;
            every = [string]$_.Repetition.Interval; for = [string]$_.Repetition.Duration; enabled = [bool]$_.Enabled } })
        last_run = $(if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) { $i.LastRunTime.ToString('s') } else { $null })
        last_result = $i.LastTaskResult
        next_run = $(if ($i.NextRunTime) { $i.NextRunTime.ToString('s') } else { $null })
    }
}
ConvertTo-Json -InputObject @($out) -Depth 5 -Compress
"""


def state() -> dict:
    """Our task, and the other tasks that run restic (an older backup script, say)."""
    if not supported():
        return {"supported": False, "ours": None, "others": []}
    done = _powershell(_STATE)
    try:
        tasks = json.loads(done.stdout or "[]")
    except ValueError:
        raise ScheduleError(f"could not read the scheduled tasks: {(done.stderr or done.stdout).strip()[:300]}") from None
    ours = next((t for t in tasks if t["name"] == TASK_NAME), None)
    return {"supported": True, "ours": ours, "others": [t for t in tasks if t["name"] != TASK_NAME]}


def run_now() -> dict:
    """Start the task now (a backup with the task's rights: VSS and all)."""
    done = _powershell(f"Start-ScheduledTask -TaskName {_quote(TASK_NAME)}")
    if done.returncode != 0:
        return {"ok": False, "error": (done.stderr or "could not start the task").strip()[:300]}
    return {"ok": True}
