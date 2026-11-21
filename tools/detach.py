#!/usr/bin/env python3
"""Launch a command fully detached, so it survives whoever started it.

Why this exists: start_services.sh is run by IINA (`utils.exec`) and sometimes by
a terminal/session that tears down its whole process group on exit. Plain
`nohup cmd &` keeps the child in that group, so it dies with the caller -- the
subtitle services then vanish a few seconds after "services ready". Starting the
child in its own session (`start_new_session=True` == setsid) makes it immune and
also detaches it from any controlling terminal.

Usage: detach.py <logfile> <cmd> [args...]     (prints the child pid)
"""
import subprocess
import sys


def main():
    if len(sys.argv) < 3:
        print("usage: detach.py <logfile> <cmd> [args...]", file=sys.stderr)
        return 2
    log_path, cmd = sys.argv[1], sys.argv[2:]
    with open(log_path, "ab", buffering=0) as log:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,   # setsid: own session, no process-group kill
            close_fds=True,
        )
    print(proc.pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
