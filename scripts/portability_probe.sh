#!/bin/sh
# portability_probe.sh - report what running-ng can and cannot do on this host.
#
# Safe and read-only apart from a Python venv it creates under /tmp. Runs no
# benchmarks. Intended for a machine running-ng has never run on, to separate
# "the harness plumbing works here" from "the performance counters work here".
#
# Usage:  sh scripts/portability_probe.sh
#
# POSIX sh on purpose: FreeBSD has no bash in the base system.

set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT" || exit 1

say() { printf '\n=== %s ===\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

say "host"
uname -a
echo "python: $(command -v python3 || echo MISSING)"
python3 --version 2>&1

say "tools on PATH"
for t in perf pmcstat pmc pmccontrol olly ocaml-processor-dump taskset cpuset; do
    printf '  %-22s %s\n' "$t" "$(command -v $t 2>/dev/null || echo '-')"
done

say "hwpmc (FreeBSD)"
if [ "$(uname -s)" = "FreeBSD" ]; then
    kldstat -m hwpmc 2>&1 || echo "hwpmc NOT loaded: run 'sudo kldload hwpmc'"
    echo "--- security.bsd.unprivileged_proc_debug (need 1 for unprivileged attach):"
    sysctl security.bsd.unprivileged_proc_debug 2>&1
else
    echo "(not FreeBSD, skipping)"
fi

# The detection section below needs no third-party packages: running.osinfo and
# running.counters import only the standard library. So it runs on the system
# python with PYTHONPATH set, and a missing venv or pytest costs us the test
# run but not the report.
PY=python3
export PYTHONPATH="$ROOT/src"

say "unit tests (OS abstraction, counter backends, topology)"
VENV=/tmp/running-ng-probe-venv
if [ ! -d "$VENV" ] && ! python3 -m venv "$VENV" >/tmp/probe-venv.log 2>&1; then
    echo "SKIPPED: could not create a venv ($(tail -1 /tmp/probe-venv.log))"
    echo "Not fatal: the detection below still runs."
    rm -rf "$VENV"
elif [ -x "$VENV/bin/pip" ]; then
    "$VENV/bin/pip" install -q -e ".[tests]" 2>&1 | tail -3
    "$VENV/bin/python" -m pytest -q \
        tests/test_osinfo.py tests/test_counters.py tests/test_cpu_topology.py \
        tests/test_freebsd_backend_integration.py 2>&1 | tail -20
else
    echo "SKIPPED: venv has no pip."
    rm -rf "$VENV"
fi

say "what running-ng detects here"
"$PY" - <<'PYEOF'
import logging
logging.basicConfig(level=logging.INFO, format="  %(levelname)s %(message)s")
from running import osinfo, counters

print("  system            :", osinfo.SYSTEM)
print("  cpu_model         :", osinfo.cpu_model() or "(unknown)")
print("  logical cpus      :", osinfo.core_count())
print("  pid->exe lookup   :", osinfo.EXE_LOOKUP_SUPPORTED)
groups = osinfo.sibling_groups()
print("  physical cores    :", len(groups))
print("  sibling groups    :", groups[:4], "..." if len(groups) > 4 else "")
bench, obs = osinfo.partition_cpus()
print("  benchmark cpus    :", osinfo.format_cpu_list(bench) or "(cannot pin)")
print("  observer cpus     :", osinfo.format_cpu_list(obs) or "(none)")
print("  pin command       :", osinfo.pin_command(bench) or "(none available)")
print("  manifest topology :", osinfo.machine_topology_summary())
b = counters.select_backend()
print("  counter backend   :", b.name, "(available=%s)" % b.available())
PYEOF

say "available PMC events (first 40)"
if have pmc; then
    pmc list 2>&1 | head -40
elif have pmccontrol; then
    pmccontrol -L 2>&1 | head -40
else
    echo "(no pmc/pmccontrol)"
fi

say "live pmcstat attach against 'sleep 3'"
# The real goal: raw pmcstat output to check the table parser against.
if have pmcstat; then
    OUT=/tmp/running-ng-probe-pmcstat.txt
    rm -f "$OUT"
    sleep 3 &
    TARGET=$!
    # Event names are the thing most likely to be wrong here: libpmc has no
    # portable aliases on modern x86. Try a few spellings and report each.
    for EV in instructions unhalted-cycles cycles; do
        rm -f "$OUT"
        pmcstat -C -d -w 1 -o "$OUT" -p "$EV" -t "$TARGET" 2>/tmp/probe-err.txt &
        PMC=$!
        sleep 1
        kill "$PMC" 2>/dev/null
        wait "$PMC" 2>/dev/null
        if [ -s "$OUT" ]; then
            printf '  event %-18s OK\n' "$EV"
        else
            printf '  event %-18s FAILED: %s\n' "$EV" "$(head -1 /tmp/probe-err.txt)"
        fi
    done
    wait "$TARGET" 2>/dev/null
    echo "--- last raw pmcstat output (this is what the parser must handle):"
    cat "$OUT" 2>/dev/null || echo "(none produced)"
    echo "--- end raw output"
else
    echo "(pmcstat not present)"
fi

say "raw topology source"
if [ "$(uname -s)" = "FreeBSD" ]; then
    sysctl -n kern.sched.topology_spec 2>&1 | head -40
elif [ "$(uname -s)" = "Linux" ]; then
    for c in 0 1; do
        printf 'cpu%s siblings: %s\n' "$c" \
            "$(cat /sys/devices/system/cpu/cpu$c/topology/thread_siblings_list 2>/dev/null)"
    done
fi

say "done"
echo "Please send back this entire output."
