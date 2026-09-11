#!/usr/bin/env python3
"""Stand-in for pmcstat(8) that reproduces its counting-mode output format.

Mirrors usr.sbin/pmcstat/pmcstat.c: field widths from lines 1160-1181, printing
from pmcstat_print_headers/print_counters (270-330). Rows every -w seconds while
the target lives, plus a final row when it exits (the SIGIO path at line 1386).
"""
import math, os, sys, time

args = sys.argv[1:]
events, out, target, interval, cumulative, descendants = [], None, None, 5.0, False, False
i = 0
while i < len(args):
    a = args[i]
    if a == "-p": events.append(args[i + 1]); i += 2
    elif a == "-o": out = args[i + 1]; i += 2
    elif a == "-t": target = int(args[i + 1]); i += 2
    elif a == "-w": interval = float(args[i + 1]); i += 2
    elif a == "-C": cumulative = True; i += 1
    elif a == "-d": descendants = True; i += 1
    else:
        sys.stderr.write("pmcstat: unknown option %s\n" % a); sys.exit(64)
if not events or out is None or target is None:
    sys.stderr.write("pmcstat: missing -p/-o/-t\n"); sys.exit(64)

# libpmc installs its portable alias table only for AMD K8, the generic class
# and a few ARM cores, so on modern x86 anything but a raw event name from
# `pmc list` fails to allocate. Model that: known aliases, or a raw name in the
# uppercase dotted style the Intel/AMD tables use.
KNOWN_ALIASES = {"instructions", "cycles", "unhalted-cycles", "branches",
                 "branch-mispredicts", "dc-misses", "ic-misses", "interrupts"}
for e in events:
    if e in KNOWN_ALIASES:
        continue
    if e and e[0].isupper() and all(c.isalnum() or c in "_." for c in e):
        continue
    sys.stderr.write(
        'pmcstat: ERROR: Cannot allocate process-mode pmc with specification '
        '"%s": No such file or directory\n' % e)
    sys.exit(69)
if not cumulative:
    sys.stderr.write("fake pmcstat: expected -C, harness relies on cumulative\n"); sys.exit(70)

def widths(name):
    hw, dw = len(name) + 2, int(math.floor(48 / 3.32193)) + 1
    return (0, hw) if hw > dw else (dw - hw, dw)

def alive(pid):
    try: os.kill(pid, 0); return True
    except OSError: return False

f = open(out, "w")
hdr = "# "
for n in events:
    skip, fw = widths(n)
    hdr += " " * skip + "p/%*s " % (fw - skip - 2, n)
f.write(hdr); f.flush()
vals, tick = [0] * len(events), 0
while True:
    running = alive(target)
    time.sleep(min(interval, 0.05))
    tick += 1
    vals = [v + 1000000 * (j + 1) for j, v in enumerate(vals)]
    line, extra = "", 2
    for n, v in zip(events, vals):
        _, fw = widths(n)
        line += "%*d " % (fw + extra, v); extra = 0
    f.write("\n" + line); f.flush()
    if not running:
        break
f.close()
