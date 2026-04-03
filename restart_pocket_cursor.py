#!/usr/bin/env python3
"""
restart_pocket_cursor.py
Kill any running PocketCursor processes and start a fresh instance.

Why a single script instead of "kill + start" as two commands?
If Cursor requires approval before running commands, killing
PocketCursor first would leave you unable to approve the start
command from your phone. This script does both in one approval —
kill the old process and immediately start a new one.

Usage:
    python restart_pocket_cursor.py
"""

import functools
import os
import subprocess
import sys
import time
from pathlib import Path

print = functools.partial(print, flush=True)

SCRIPT_DIR = Path(__file__).parent
POCKET_CURSOR_SCRIPT = SCRIPT_DIR / 'pocket_cursor.py'


def find_pids_wmic():
    """Find PIDs via wmic (missing on some Windows installs)."""
    try:
        result = subprocess.run(
            [
                'wmic',
                'process',
                'where',
                "commandline like '%pocket_cursor.py%' and not commandline like '%wmic%' and not commandline like '%restart%'",
                'get',
                'processid',
            ],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=15,
        )
        pids = []
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
        return pids
    except Exception as e:
        print(f'Warning: wmic query failed: {e}')
        return []


def find_pids_powershell():
    """Fallback when wmic is unavailable — same logic as wmic."""
    ps = (
        'Get-CimInstance Win32_Process | Where-Object { '
        "$_.CommandLine -match 'pocket_cursor[.]py' -and "
        "$_.CommandLine -notmatch 'restart_pocket_cursor' -and "
        "$_.CommandLine -notmatch 'wmic' "
        '} | ForEach-Object { $_.ProcessId }'
    )
    try:
        result = subprocess.run(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=30,
        )
        pids = []
        for line in (result.stdout or '').splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
        return pids
    except Exception as e:
        print(f'Warning: PowerShell process query failed: {e}')
        return []


def find_pids():
    """All PIDs running pocket_cursor.py (bridge only, not restart script)."""
    merged = set(find_pids_wmic()) | set(find_pids_powershell())
    return sorted(merged)


def kill_processes(pids):
    """Kill PocketCursor processes with /T (process tree)."""
    for pid in pids:
        try:
            result = subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(pid)],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=10,
            )
            print(f'  Killed PID {pid}: {result.stdout.strip()}')
        except Exception as e:
            print(f'  Failed to kill PID {pid}: {e}')


def main():
    # 1. Find and kill existing PocketCursor processes (multiple instances = duplicate Telegram messages)
    pids = find_pids()
    if pids:
        print(f'Found {len(pids)} PocketCursor process(es): {pids}')
        kill_processes(pids)
        time.sleep(0.8)
        still = find_pids()
        if still:
            print(f'WARNING: Still running after taskkill: {still} — retrying...')
            kill_processes(still)
            time.sleep(0.8)
    else:
        print('No running PocketCursor found.')

    # 2. Remove lock only after kill attempts (old script deleted lock even when kill failed → many bridges)
    lock_file = SCRIPT_DIR / '.bridge.lock'
    if lock_file.exists():
        lock_file.unlink()
        print('Removed lock file before start.')

    # 3. Start PocketCursor as subprocess and wait (keeps parent alive so
    #    Cursor terminal tracking is preserved — os.execv on Windows
    #    spawns a new process and exits, which orphans it).
    print(f'\nStarting PocketCursor from {SCRIPT_DIR}...')
    os.chdir(SCRIPT_DIR)
    proc = subprocess.Popen([sys.executable, '-X', 'utf8', str(POCKET_CURSOR_SCRIPT)])
    sys.exit(proc.wait())


if __name__ == '__main__':
    main()
