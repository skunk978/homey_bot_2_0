"""
Stop existing Homey bot processes before starting a new instance.

On Windows, finds python.exe command lines tied to this project (main bot,
Discord voice/transcribe workers, and multiprocessing children under this folder).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

def _project_root() -> Path:
    return Path(__file__).resolve().parent


def _command_line_matches_homey_bot(cmd: str) -> bool:
    """Match only real bot/worker entrypoints (not arbitrary -c snippets in this folder)."""
    if not cmd:
        return False
    lower = cmd.lower()
    if "homey_bot_space_lord.py" in lower:
        return True
    if "discord_voice_listen_process" in lower:
        return True
    if "discord_transcribe_process" in lower:
        return True
    if "multiprocessing.spawn" in lower and "homey_bot_2_0" in lower:
        return True
    return False


def _list_homey_bot_pids_windows() -> list[tuple[int, str]]:
    """Return (pid, commandline_prefix) for matching python.exe processes."""
    script = (
        "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" -ErrorAction SilentlyContinue | "
        "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        log.debug("[BotGuard] PowerShell process listing failed", exc_info=True)
        return []
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return []
    import json

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    out: list[tuple[int, str]] = []
    for row in data:
        try:
            pid = int(row.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        cmd = str(row.get("CommandLine") or "")
        if pid > 0 and _command_line_matches_homey_bot(cmd):
            out.append((pid, cmd[:120]))
    return out


def _list_homey_bot_pids_posix() -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    try:
        proc = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return out
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        cmd = parts[1]
        if _command_line_matches_homey_bot(cmd):
            out.append((pid, cmd[:120]))
    return out


def list_homey_bot_processes() -> list[tuple[int, str]]:
    if os.name == "nt":
        return _list_homey_bot_pids_windows()
    return _list_homey_bot_pids_posix()


def _get_parent_pid(pid: int) -> int | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        script = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' -ErrorAction SilentlyContinue).ParentProcessId"
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0 and (proc.stdout or "").strip().isdigit():
                return int(proc.stdout.strip())
        except Exception:
            pass
        return None
    try:
        return int(os.getppid()) if pid == os.getpid() else None
    except Exception:
        return None


def _terminate_pid(pid: int) -> None:
    """Terminate one process by PID (never the current process)."""
    pid = int(pid)
    if pid <= 0 or pid == os.getpid():
        return
    if os.name == "nt":
        import ctypes

        PROCESS_TERMINATE = 0x0001
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not handle:
            return
        try:
            kernel32.TerminateProcess(handle, 1)
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def stop_existing_bot_processes(
    *,
    except_pid: int | None = None,
    settle_seconds: float = 2.0,
) -> int:
    """
    Terminate other Homey bot / worker Python processes.

    Returns the number of processes we attempted to stop.
    """
    me = int(except_pid if except_pid is not None else os.getpid())
    my_parent = _get_parent_pid(me)
    targets: list[tuple[int, str]] = []
    for pid, cmd in list_homey_bot_processes():
        ipid = int(pid)
        if ipid == me or ipid == os.getpid():
            continue
        if my_parent and ipid == my_parent:
            log.info("[BotGuard] Skip stop of parent PID %s", ipid)
            continue
        targets.append((ipid, cmd))
    if not targets:
        _remove_stale_lock_file()
        return 0

    log.warning(
        "[BotGuard] Stopping %s existing bot process(es) before start (new PID %s): %s",
        len(targets),
        me,
        ", ".join(str(p) for p, _ in targets),
    )
    for pid, cmd in targets:
        if int(pid) == os.getpid():
            log.warning("[BotGuard] Skip stop for current PID %s", pid)
            continue
        log.info("[BotGuard] Stopping PID %s (%s…)", pid, cmd[:80])
        _terminate_pid(pid)

    if settle_seconds > 0:
        time.sleep(settle_seconds)

    remaining = [
        pid
        for pid, _ in list_homey_bot_processes()
        if int(pid) != me and int(pid) != os.getpid()
    ]
    if remaining:
        log.warning("[BotGuard] Still running after stop: %s — retrying force kill", remaining)
        for pid in remaining:
            _terminate_pid(pid)
        time.sleep(settle_seconds)

    _remove_stale_lock_file()
    return len(targets)


def _run_under_startup_lock(callback) -> None:
    """Serialize stop+cleanup so two simultaneous starts do not kill each other."""
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        mutex = kernel32.CreateMutexW(None, False, "Local\\HomeyBot20_StartupMutex")
        if not mutex:
            callback()
            return
        wait_ms = 60_000
        rc = kernel32.WaitForSingleObject(mutex, wait_ms)
        if rc not in (0, 0x80):  # WAIT_OBJECT_0, WAIT_ABANDONED
            log.warning("[BotGuard] Startup mutex wait failed (rc=%s); continuing", rc)
        try:
            callback()
        finally:
            kernel32.ReleaseMutex(mutex)
            kernel32.CloseHandle(mutex)
        return

    lock_path = _project_root() / ".homey_bot.startup.lock"
    with open(lock_path, "a+b") as lock_file:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            callback()
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _remove_stale_lock_file() -> None:
    lock = _project_root() / ".homey_bot.pid"
    try:
        lock.unlink(missing_ok=True)
    except TypeError:
        try:
            if lock.exists():
                lock.unlink()
        except OSError:
            pass
    except OSError:
        pass
