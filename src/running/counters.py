"""Hardware counter backends.

running-ng attaches a counter tool to a benchmark process that is held blocked
until the tool is running.  Which tool that is depends on the OS:

  * ``linux-perf``   -- ``perf stat --json --inherit -p PID``
  * ``freebsd-pmc``  -- ``pmcstat -C -d -t PID -p EVENT``
  * ``none``         -- no counters; olly and rusage still run

All three attach to a PID we already own, so the harness keeps the benchmark as
its own direct child: its exit status stays meaningful (crash detection), its
stderr stays separate, and a timeout can still kill it.  A launcher-style tool
(macOS mperf, ``pmc stat``) would take that ownership away, which is why
neither is used here.

Backends normalise to one record shape, ``{"event": str, "counter-value":
float}``, matching what ``contract.emit.perf_metrics`` already consumes.  That
is not an attempt to imitate perf: it is the minimal (name, value) pair, and
reusing it means a new backend needs no contract change for events the
vocabulary already knows.  Raw tool spellings are mapped onto the canonical
name by each backend's ``EVENT_ALIASES``.
"""
import logging
import os
import re
import select
import subprocess
import time
from typing import Dict, List, Optional, Sequence, Tuple

from running import osinfo

#: Env var forcing a specific backend, mostly for testing a degraded path on a
#: host that could run a richer one.  Accepts any registered backend name.
BACKEND_ENV_VAR = "RUNNING_NG_COUNTER_BACKEND"


class CounterHandle:
    """A running counter tool attached to one benchmark process."""

    def __init__(self, proc: subprocess.Popen, output_path: str,
                 ctl_fds: Sequence[Optional[int]] = ()):
        self.proc = proc
        self.output_path = output_path
        self.ctl_fds = tuple(ctl_fds)


class CounterBackend:
    """No counters.  Also the base class, and the documented degraded mode.

    A platform with no usable counter tool is a first-class configuration:
    olly still gives GC metrics and rusage still gives CPU time and faults,
    which is most of what a GC sweep actually reads.
    """

    name = "none"
    #: Raw tool event name -> canonical name used by the contract vocabulary.
    EVENT_ALIASES: Dict[str, str] = {}

    def available(self) -> bool:
        return True

    def attach(self, pid: int, tmpdir: str, events: Sequence[str],
               tag: str = "main",
               pin_prefix: Sequence[str] = ()) -> Optional[CounterHandle]:
        return None

    def stop(self, handle: Optional[CounterHandle], timeout: float = 10.0) -> None:
        """Release the tool.  Both real backends exit on their own when the
        target does, so this only bounds the wait and cleans up control fds."""
        if handle is None:
            return
        for fd in handle.ctl_fds:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        try:
            handle.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            handle.proc.kill()
            handle.proc.wait()

    def kill(self, handle: Optional[CounterHandle]) -> None:
        """Tear down without collecting, for a re-attach to a different PID."""
        if handle is None:
            return
        handle.proc.kill()
        handle.proc.wait()
        for fd in handle.ctl_fds:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def collect(self, handle: Optional[CounterHandle]) -> List[Dict]:
        return []

    def _canonical(self, raw: str) -> str:
        return self.EVENT_ALIASES.get(raw, raw)


# --- Linux: perf ---------------------------------------------------------------

# Seconds to wait for `perf stat --control` to acknowledge `enable`.
PERF_ARM_TIMEOUT = 10.0

def _start_perf_armed(perf_cmd: List[str], tmpdir: str, tag: str,
                      skip: int = 0) -> Tuple[subprocess.Popen, Optional[int], Optional[int]]:
    """Start `perf stat` and return only once its counters are armed.

    `Popen` returning means perf was forked, not that it has called
    perf_event_open.  Releasing the benchmark at that point is a race: if perf
    finishes its own startup after the target has spawned threads, those
    threads are counted by nobody — they were absent from perf's thread map and
    `inherit` only follows tasks created after the event exists.  The symptom is
    a whole-process measurement that reports one thread's worth of task-clock
    (owl_gc: 10.5s instead of 322s, and 90k page faults instead of 197k), which
    silently under-reports every counter for any threaded benchmark.

    So start perf with events disabled (`--delay -1`) and drive its control
    protocol: write `enable` and block until perf answers `ack`.  Only then may
    the caller let the benchmark run.  Both fifos are opened O_RDWR here so
    neither side blocks on the other's open(), as in perf-stat(1)'s own example.

    Returns (process, ctl_fd, ack_fd); the fds are the caller's to close.  If
    the handshake fails (perf too old for --control, perf died on a bad event
    list), perf is restarted without it and (process, None, None) is returned —
    degraded to the old racy behaviour rather than failing the run.
    """
    ctl_path = os.path.join(tmpdir, "perf_ctl_{}.fifo".format(tag))
    ack_path = os.path.join(tmpdir, "perf_ack_{}.fifo".format(tag))
    ctl_fd = ack_fd = None
    try:
        os.mkfifo(ctl_path)
        os.mkfifo(ack_path)
        # O_RDWR on a fifo never blocks, and holding both ends keeps perf's own
        # open() from blocking whichever order it opens them in.
        ctl_fd = os.open(ctl_path, os.O_RDWR)
        ack_fd = os.open(ack_path, os.O_RDWR)
        # Options go after the `stat` subcommand, so splice at index 2 --
        # past any pin prefix (`taskset -c ...`) prepended by the caller.
        armed_cmd = perf_cmd[:skip + 2] + ["--delay", "-1",
                                    "--control", "fifo:{},{}".format(ctl_path, ack_path)] + perf_cmd[skip + 2:]
        p = subprocess.Popen(armed_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        os.write(ctl_fd, b"enable\n")
        # perf answers "ack\n" once the events are enabled.  Bound the wait: a
        # perf that never acks must not hang the whole run.
        deadline = time.time() + PERF_ARM_TIMEOUT
        buf = b""
        while time.time() < deadline:
            if p.poll() is not None:
                raise RuntimeError("perf exited with {} before acking".format(p.returncode))
            r, _, _ = select.select([ack_fd], [], [], 0.05)
            if r:
                buf += os.read(ack_fd, 64)
                if b"ack" in buf:
                    return p, ctl_fd, ack_fd
        raise RuntimeError("perf did not ack within {}s".format(PERF_ARM_TIMEOUT))
    except (OSError, RuntimeError) as e:
        logging.warning(
            "perf --control handshake failed (%s); falling back to an unsynchronised "
            "attach. Counters for threaded benchmarks may under-report.", e)
        for fd in (ctl_fd, ack_fd):
            if fd is not None:
                os.close(fd)
        try:
            p.kill()
            p.wait()
        except (NameError, UnboundLocalError, OSError):
            pass
        return subprocess.Popen(perf_cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT), None, None


class PerfBackend(CounterBackend):
    name = "linux-perf"

    def available(self) -> bool:
        return osinfo.IS_LINUX and _tool_on_path("perf")

    def attach(self, pid: int, tmpdir: str, events: Sequence[str],
               tag: str = "main",
               pin_prefix: Sequence[str] = ()) -> Optional[CounterHandle]:
        output = os.path.join(tmpdir, "perf.json")
        cmd = ["perf", "stat", "--json", "--inherit", "-p", str(pid), "-o", output]
        if events:
            cmd.extend(["-e", ",".join(events)])
        # _start_perf_armed splices its own options in after the subcommand, so
        # it needs to know how many leading tokens are the pin prefix.
        proc, ctl_fd, ack_fd = _start_perf_armed(
            list(pin_prefix) + cmd, tmpdir, tag, skip=len(pin_prefix))
        return CounterHandle(proc, output, (ctl_fd, ack_fd))

    def collect(self, handle: Optional[CounterHandle]) -> List[Dict]:
        if handle is None:
            return []
        try:
            with open(handle.output_path, "r") as f:
                return parse_perf_ndjson(f.read())
        except FileNotFoundError:
            logging.warning("perf output file %s not found", handle.output_path)
            return []


def parse_perf_ndjson(text: str) -> List[Dict]:
    """`perf stat --json` emits one JSON object per counter, not one document."""
    import json
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


# --- FreeBSD: pmcstat ----------------------------------------------------------

#: Matches one column label in a pmcstat header: "p/instructions" for a
#: process-scope PMC, "s/03/instructions" for a system-scope one on cpu 3.
_PMCSTAT_COLUMN = re.compile(r"^[ps]/(?:\d+/)?(?P<name>.+)$")


def parse_pmcstat_table(text: str) -> Dict[str, int]:
    """Return the final cumulative counter values from pmcstat's output.

    pmcstat prints a time series, not a total: a `# p/<event>` header followed
    by right-aligned columns, one row per `-w` interval plus a final row when
    the target exits, with the header reprinted every 256 rows.  Run with `-C`
    the values are cumulative, so the last complete row is the run total.

    Returns {} when there is no usable row, which is what a failed PMC
    allocation looks like from here.
    """
    names: List[str] = []
    last: Dict[str, int] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            parsed = []
            for token in stripped.lstrip("#").split():
                m = _PMCSTAT_COLUMN.match(token)
                if m:
                    parsed.append(m.group("name"))
            # A header we cannot parse must not be silently paired with the
            # rows beneath it: that would mislabel every counter.
            names = parsed if parsed else []
            continue
        if not names:
            continue
        tokens = stripped.split()
        if len(tokens) != len(names):
            continue
        try:
            values = [int(t) for t in tokens]
        except ValueError:
            continue
        last = dict(zip(names, values))
    return last


def split_event_list(events: Sequence[str]) -> List[str]:
    """Flatten an event list into one name per element.

    Configs write events the way perf takes them, as a single comma-separated
    string ("task-clock,cycles,instructions"), and the modifier splits only on
    whitespace, so one element can hold several events.  perf does not care
    because we join with commas again; pmcstat needs a separate -p per event,
    so it has to be flattened first.
    """
    out: List[str] = []
    for chunk in events:
        for name in str(chunk).split(","):
            name = name.strip()
            if name:
                out.append(name)
    return out


class PmcStatBackend(CounterBackend):
    """FreeBSD hwpmc(4) via pmcstat(8).

    Needs no privileges: hwpmc requires root only for system-scope PMCs, while
    process-scope ones just need p_candebug(9) on the target (same uid, with
    security.bsd.unprivileged_proc_debug at its default of 1).  It does need
    the module loaded: `kldload hwpmc`, or hwpmc_load="YES" in loader.conf.

    Event names are NOT portable.  libpmc only installs its alias table
    ("instructions", "cycles", ...) for AMD K8, the generic class and a few ARM
    cores, so on a modern Intel or Zen host those names do not resolve and the
    events must be named explicitly from `pmc list`.  A bad event list makes
    pmcstat exit immediately; that is reported loudly and the invocation
    continues without counters rather than failing the sweep.
    """

    name = "freebsd-pmc"

    #: pmcstat spellings that mean the same thing as a perf event we already
    #: map in the contract vocabulary.  Extend as real hardware is tested.
    EVENT_ALIASES = {
        "unhalted-cycles": "cycles",
        "tsc": "cycles",
    }

    #: Print interval.  pmcstat emits nothing until the first tick, so this is
    #: also the granularity of the free time series we get alongside the total.
    #: pmcstat's own default of 5s would lose short benchmarks entirely, since
    #: their only row would be the final one.
    INTERVAL_SECONDS = 1.0

    #: Only meaningful where libpmc installs an alias table.  Deliberately not
    #: silently substituted on other hardware: a wrong event is worse than a
    #: missing one, so let pmcstat reject it and say so.
    DEFAULT_EVENTS = ("instructions", "unhalted-cycles")

    def available(self) -> bool:
        return osinfo.IS_FREEBSD and _tool_on_path("pmcstat")

    def attach(self, pid: int, tmpdir: str, events: Sequence[str],
               tag: str = "main",
               pin_prefix: Sequence[str] = ()) -> Optional[CounterHandle]:
        output = os.path.join(tmpdir, "pmcstat_{}.txt".format(tag))
        chosen = split_event_list(events) or list(self.DEFAULT_EVENTS)
        # -C (cumulative) and -d (count descendants, the --inherit equivalent)
        # are toggles that must precede the -p they apply to.
        cmd = list(pin_prefix) + ["pmcstat", "-C", "-d",
                                  "-w", str(self.INTERVAL_SECONDS), "-o", output]
        for ev in chosen:
            cmd.extend(["-p", ev])
        cmd.extend(["-t", str(pid)])
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)
        return CounterHandle(proc, output)

    def collect(self, handle: Optional[CounterHandle]) -> List[Dict]:
        if handle is None:
            return []
        if handle.proc.returncode not in (0, None):
            stderr = b""
            try:
                stderr = handle.proc.stderr.read() if handle.proc.stderr else b""
            except (OSError, ValueError):
                pass
            logging.warning(
                "pmcstat exited %s; no counters for this invocation. Check the "
                "event names against `pmc list` (libpmc has no portable aliases "
                "on modern x86) and that hwpmc is loaded. stderr: %s",
                handle.proc.returncode,
                stderr.decode("utf-8", "replace").strip()[:400])
            return []
        try:
            with open(handle.output_path, "r") as f:
                table = parse_pmcstat_table(f.read())
        except FileNotFoundError:
            logging.warning("pmcstat output file %s not found", handle.output_path)
            return []
        if not table:
            logging.warning(
                "pmcstat produced no complete counter row in %s; the benchmark "
                "may have finished inside the first %.1fs interval",
                handle.output_path, self.INTERVAL_SECONDS)
        return [{"event": self._canonical(name), "counter-value": float(value)}
                for name, value in table.items()]


# --- selection -----------------------------------------------------------------

def _tool_on_path(name: str) -> bool:
    import shutil
    return shutil.which(name) is not None


_BACKENDS = (PerfBackend, PmcStatBackend, CounterBackend)


def select_backend() -> CounterBackend:
    """The richest backend this host can actually run.

    Falls back to `none` rather than raising: a host without a counter tool
    should still produce olly and rusage data, not fail its sweep.
    """
    forced = os.environ.get(BACKEND_ENV_VAR, "").strip()
    if forced:
        for cls in _BACKENDS:
            if cls.name == forced:
                backend = cls()
                if not backend.available():
                    logging.warning(
                        "%s forced to %r, which reports itself unavailable on this "
                        "host; using it anyway", BACKEND_ENV_VAR, forced)
                return backend
        raise ValueError("Unknown {}={!r}; known backends: {}".format(
            BACKEND_ENV_VAR, forced, ", ".join(c.name for c in _BACKENDS)))
    for cls in _BACKENDS:
        backend = cls()
        if backend.available():
            return backend
    return CounterBackend()
