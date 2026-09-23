"""Hardware counter backends: ``linux-perf``, ``freebsd-pmc``, ``none``.

Every backend attaches to a PID the harness owns (never a launcher-style tool),
so the benchmark stays a direct child with a meaningful exit status. Output is
normalised to ``{"event": str, "counter-value": float}`` records, with raw tool
spellings mapped to canonical names via ``EVENT_ALIASES``.
"""
import logging
import os
import re
import select
import subprocess
import time
from typing import Dict, List, Optional, Sequence, Tuple

from running import osinfo

#: Env var forcing a specific backend (any registered backend name).
BACKEND_ENV_VAR = "RUNNING_NG_COUNTER_BACKEND"


class CounterHandle:
    """A running counter tool attached to one benchmark process."""

    def __init__(self, proc: subprocess.Popen, output_path: str,
                 ctl_fds: Sequence[Optional[int]] = ()):
        self.proc = proc
        self.output_path = output_path
        self.ctl_fds = tuple(ctl_fds)
        #: Set by stop() when the tool had to be killed; output that is only
        #: complete at exit must then be treated as no result.
        self.killed = False
        #: Cleared by wait_ready when the tool was not confirmed counting before
        #: the benchmark was released.
        self.ready = True


class CounterBackend:
    """No counters: the base class and the degraded mode (olly and rusage still run)."""

    name = "none"
    #: Raw tool event name -> canonical contract name.
    EVENT_ALIASES: Dict[str, str] = {}

    def available(self) -> bool:
        return True

    def attach(self, pid: int, tmpdir: str, events: Sequence[str],
               tag: str = "main",
               pin_prefix: Sequence[str] = ()) -> Optional[CounterHandle]:
        return None

    def wait_ready(self, handle: Optional[CounterHandle],
                   timeout: float = 5.0) -> bool:
        """Block until the tool is counting; perf arms itself in attach(), so nothing to do by default."""
        return True

    def stop(self, handle: Optional[CounterHandle], timeout: float = 10.0) -> None:
        """Bound the wait for the tool (it exits with the target) and clean up control fds."""
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
            logging.warning(
                "counter tool did not exit within %ss of the benchmark; killing it. "
                "Its output may be incomplete.", timeout)
            handle.killed = True
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

PERF_ARM_TIMEOUT = 10.0

def _start_perf_armed(perf_cmd: List[str], tmpdir: str, tag: str,
                      skip: int = 0) -> Tuple[subprocess.Popen, Optional[int], Optional[int]]:
    """Start `perf stat` and return only once its counters are armed.

    Popen returning does not mean perf has called perf_event_open; threads the
    target spawns before that are never counted (`inherit` only follows tasks
    created after the event exists). So perf starts with `--delay -1` and is
    enabled through its `--control` fifos, blocking on the `ack`.

    Returns (process, ctl_fd, ack_fd); the fds are the caller's to close. If the
    handshake fails (old perf, bad event list), perf is restarted without it and
    (process, None, None) is returned.
    """
    ctl_path = os.path.join(tmpdir, "perf_ctl_{}.fifo".format(tag))
    ack_path = os.path.join(tmpdir, "perf_ack_{}.fifo".format(tag))
    ctl_fd = ack_fd = None
    try:
        os.mkfifo(ctl_path)
        os.mkfifo(ack_path)
        # O_RDWR on a fifo never blocks, and keeps perf's own open() from blocking.
        ctl_fd = os.open(ctl_path, os.O_RDWR)
        ack_fd = os.open(ack_path, os.O_RDWR)
        # Options go after the `stat` subcommand, past any pin prefix.
        armed_cmd = perf_cmd[:skip + 2] + ["--delay", "-1",
                                    "--control", "fifo:{},{}".format(ctl_path, ack_path)] + perf_cmd[skip + 2:]
        p = subprocess.Popen(armed_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        os.write(ctl_fd, b"enable\n")
        # perf answers "ack\n" once the events are enabled.
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

#: A pmcstat header column: "p/instructions" (process scope) or "s/03/instructions" (cpu 3).
_PMCSTAT_COLUMN = re.compile(r"^[ps]/(?:\d+/)?(?P<name>.+)$")


def parse_pmcstat_table(text: str) -> Dict[str, int]:
    """Final cumulative counter values from pmcstat's output, or {} if no usable row.

    Only the row written at target exit is trustworthy: hwpmc saves a
    process-scope counter only on context switch, so the intermediate `-w`
    rows are stale snapshots, not a time series. The header is reprinted every 256 rows.
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
            # an unparsable header must not be paired with the rows beneath it
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
    """Flatten comma-separated event strings into one name per element; pmcstat needs a separate -p per event."""
    out: List[str] = []
    for chunk in events:
        for name in str(chunk).split(","):
            name = name.strip()
            if name:
                out.append(name)
    return out


class PmcStatBackend(CounterBackend):
    """FreeBSD hwpmc(4) via pmcstat(8).

    Process-scope PMCs need no root, only the hwpmc module loaded (`kldload hwpmc`).
    Event names are only partly portable ("cycles", "task-clock", "branch-misses",
    "cache-misses" do not resolve; "unhalted-cycles" does; see `pmc list-events`),
    and are never silently substituted: a bad list makes pmcstat exit and costs
    the invocation its counters.
    """

    name = "freebsd-pmc"

    #: pmcstat spellings of perf events the contract vocabulary already maps.
    EVENT_ALIASES = {
        "unhalted-cycles": "cycles",
        "tsc": "cycles",
        # a soft PMC (pmc.soft(3)) fired from the page-fault handler
        "PAGE_FAULT.ALL": "page-faults",
    }

    #: Print interval; only the exit row is read, so this just keeps a stalled run visible.
    INTERVAL_SECONDS = 1.0

    #: Verified on FreeBSD 15.1 / Xeon E5-2640 v4.
    DEFAULT_EVENTS = ("instructions", "unhalted-cycles")

    def available(self) -> bool:
        return osinfo.IS_FREEBSD and _tool_on_path("pmcstat")

    def attach(self, pid: int, tmpdir: str, events: Sequence[str],
               tag: str = "main",
               pin_prefix: Sequence[str] = ()) -> Optional[CounterHandle]:
        output = os.path.join(tmpdir, "pmcstat_{}.txt".format(tag))
        chosen = split_event_list(events) or list(self.DEFAULT_EVENTS)
        # -C (cumulative) and -d (count descendants) must precede the -p they apply to.
        cmd = list(pin_prefix) + ["pmcstat", "-C", "-d",
                                  "-w", str(self.INTERVAL_SECONDS), "-o", output]
        for ev in chosen:
            cmd.extend(["-p", ev])
        cmd.extend(["-t", str(pid)])
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)
        return CounterHandle(proc, output)

    def wait_ready(self, handle: Optional[CounterHandle],
                   timeout: float = 5.0) -> bool:
        """Wait until pmcstat has written its header, which it does only after
        allocating the PMC and attaching. Releasing the benchmark earlier
        zeroes or under-counts roughly half of invocations.
        """
        if handle is None:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if handle.proc.poll() is not None:
                # died on its own; collect() reports its stderr
                return False
            try:
                with open(handle.output_path, "r") as f:
                    if f.read(1) == "#":
                        return True
            except (OSError, ValueError):
                pass
            time.sleep(0.01)
        handle.ready = False
        logging.warning(
            "pmcstat did not start counting within %.1fs; releasing the "
            "benchmark anyway, but this invocation's counters will be "
            "discarded as untrustworthy", timeout)
        return False

    def collect(self, handle: Optional[CounterHandle]) -> List[Dict]:
        if handle is None:
            return []
        if handle.killed:
            # A killed pmcstat leaves only a stale mid-run row that looks
            # plausible but can be wrong by a large factor (see parse_pmcstat_table).
            logging.warning(
                "pmcstat was killed before it could write its final row, so its "
                "totals for this invocation are a stale mid-run snapshot. "
                "Discarding them; this invocation has no counter data.")
            return []
        if handle.proc.returncode not in (0, None):
            stderr = b""
            try:
                stderr = handle.proc.stderr.read() if handle.proc.stderr else b""
            except (OSError, ValueError):
                pass
            logging.warning(
                "pmcstat exited %s; no counters for this invocation. Check the "
                "event names against `pmc list-events` (not every portable alias "
                "resolves: `cycles` does not, `unhalted-cycles` does) and that "
                "hwpmc is loaded. stderr: %s",
                handle.proc.returncode,
                stderr.decode("utf-8", "replace").strip()[:400])
            return []
        if not handle.ready:
            # The totals cover an unknown part of the run; a gap beats a plausible under-count.
            logging.warning(
                "pmcstat was never confirmed to be counting for this "
                "invocation; discarding its totals rather than publishing a "
                "partial count.")
            return []
        try:
            with open(handle.output_path, "r") as f:
                table = parse_pmcstat_table(f.read())
        except FileNotFoundError:
            logging.warning("pmcstat output file %s not found", handle.output_path)
            return []
        if table and not any(table.values()):
            # All-zero means the PMC never counted (pmcstat still exits 0).
            # Safe while every group carries instructions or cycles.
            logging.warning(
                "pmcstat reported zero for every counter in %s; treating as no "
                "data rather than publishing zeros", handle.output_path)
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
    """The richest backend this host can run; falls back to `none` rather than raising."""
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
