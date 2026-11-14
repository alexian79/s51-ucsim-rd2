#!/usr/bin/env python3
"""Run a console-driving shell pipeline so that nothing of it survives.

Every suite that has to survive `run' freezing the console drives the simulator
through a pipeline -- `( printf ... ; sleep 1 ) | ucsim_51 -c -' -- because a
plain `input=' closes stdin and a stopped firmware never reads the `exit'.  That
shape puts the simulator in the CHILD of `sh': on a timeout, `subprocess.run'
kills the shell and the simulator is reparented to init, still holding a CPU at
100 % on the now-closed stdin.  wptrtest hits this on purpose -- its control leg
is a sled whose watchpoint never fires, so the session is *expected* to be the
timeout -- and every floor run stranded one spinning core behind it.

Starting the shell in its own session makes it the process-group leader, so the
group can be signalled and the simulator goes with it.
"""
import os
import signal
import subprocess
from collections import namedtuple

Result = namedtuple("Result", "out killed rc")


def run_pipe(argv, timeout, **kw):
    """Popen `argv' (a shell pipeline) in a new session, return Result(out, killed, rc).

    `out' is stdout and stderr merged, which is what every caller here wants: the
    echoed command stream interleaves with the simulator's own output, so the
    suites key on substrings, never on line starts.  On timeout the whole process
    group gets SIGKILL and Result.out holds whatever had been printed by then.
    Keyword arguments are passed through to Popen.
    """
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, start_new_session=True, **kw)
    try:
        out, _ = p.communicate(timeout=timeout)
        return Result(out, False, p.returncode)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except OSError:
            pass                        # already gone; nothing to reap
        out, _ = p.communicate()
        return Result(out, True, p.returncode)
