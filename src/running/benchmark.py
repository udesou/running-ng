import errno
import glob
import json
import logging
import re
import resource
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from time import sleep
from typing import Any, List, Optional, Tuple, Union, Dict
from running.runtime import D8, JavaScriptCore, OCaml, OxCaml, OpenJDK, Runtime, DummyRuntime, SpiderMonkey
from running.modifier import *
from running.util import smart_quote, split_quoted
from pathlib import Path
from copy import deepcopy
from running import suite
import os
from enum import Enum
import pty

COMPANION_WAIT_START = 2.0

# Seconds to wait for `perf stat --control` to acknowledge `enable`.
PERF_ARM_TIMEOUT = 10.0

def _start_perf_armed(perf_cmd: List[str], tmpdir: str, tag: str) -> Tuple[subprocess.Popen, Optional[int], Optional[int]]:
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
        # Options go after the `stat` subcommand, so splice at index 2.
        armed_cmd = perf_cmd[:2] + ["--delay", "-1",
                                    "--control", "fifo:{},{}".format(ctl_path, ack_path)] + perf_cmd[2:]
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





class SubprocessrExit(Enum):
    Normal = 1
    Error = 2
    Timeout = 3
    Dryrun = 4


class Benchmark(object):
    def __init__(self, suite_name: str, name: str, wrapper: Optional[str] = None, timeout: Optional[int] = None, override_cwd: Optional[Path] = None, companion: Optional[str] = None, expected_exit: int = 0, **kwargs):
        self.name = name
        self.suite_name = suite_name
        # The exit code a *successful* run of this benchmark returns. Almost
        # always 0, but for some workloads a non-zero exit is the by-design
        # outcome rather than a failure — see `_exit_is_expected`.
        self.expected_exit: int = int(expected_exit)
        self.env_args: Dict[str, str]
        self.env_args = {}
        self.wrapper: List[str]
        if wrapper is not None:
            self.wrapper = split_quoted(wrapper)
        else:
            self.wrapper = []
        if companion is not None:
            self.companion = split_quoted(companion)
        else:
            self.companion = []
        self.perf_and_olly_attach: Optional[PerfAndOllyAttach] = None
        self.memtrace_attach: Optional[MemtraceAttach] = None
        self.timeout = timeout
        # ignore the current working directory provided by commands like runbms or minheap
        # certain benchmarks expect to be invoked from certain directories
        self.override_cwd = override_cwd
        # Per-benchmark OCAMLRUNPARAM (e.g. "e=25,d=2"), declared in the suite.
        # Applied in attach_modifiers AFTER the config-string OCamlRunParam
        # modifiers (re/md), overriding the SAME keys — so a benchmark that needs
        # its own runtime_events ring / max-domains (a bursty allocator that
        # overflows the default ring, or a multicore workload that needs > 2
        # domains) wins over whatever a global config string sets, without touching
        # that config. This is the hook that lets re/md move out of the configs and
        # be probed per-benchmark; keys a benchmark doesn't set fall through to the
        # config.
        self.ocamlrunparam: str = str(kwargs.get("ocamlrunparam", "") or "")

    def get_env_str(self) -> str:
        return " ".join([
            "{}={}".format(k, smart_quote(v))
            for (k, v) in self.env_args.items()
        ])

    def get_full_args(self, _runtime: Runtime) -> List[Union[str, Path]]:
        # makes a copy because the subclass might change the list
        # also to type check https://mypy.readthedocs.io/en/stable/common_issues.html#variance
        return list(self.wrapper)

    def prepare(self, _runtime: Runtime):
        # Optional pre-run preparation hook. Most benchmarks do nothing.
        return

    def _exit_is_expected(self, returncode: Optional[int]) -> bool:
        """Did this invocation finish the way a *successful* run should?

        `None` means the process was still running when we stopped looking,
        which the callers treat as normal.  Otherwise compare against
        `expected_exit` (0 unless the benchmark declares otherwise).

        Some workloads exit non-zero by design, and treating that as a crash
        loses their data: `alt_ergo_unsat_smt2` runs with `--timelimit 15`, so
        the workload *is* "solve for 15 seconds" — alt-ergo arms SIGVTALRM, the
        goal never closes, and the process dies of its own signal with 128+14 =
        142 on every single run.  Its olly/perf output is complete and correct,
        but the native contract emitter drops invocations the runner calls
        crashed, so the benchmark silently never reached the dashboard while the
        legacy adapter (which reads the sidecars) kept it — the two contract
        paths disagreed by exactly those cells.

        `macro-benches` already declares this in `benchmarks/manifest.yml` as
        `expected_exit: 142`; the field is spelled the same here so the two
        lists stay mechanically diffable.
        """
        if returncode is None:
            return True
        return returncode == self.expected_exit

    def attach_modifiers(self, modifiers: List[Modifier]) -> Any:
        b = deepcopy(self)
        for m in modifiers:
            if self.suite_name in m.excludes:
                if self.name in m.excludes[self.suite_name]:
                    continue
            elif type(m) == Wrapper:
                b.wrapper.extend(m.val)
            elif type(m) == Companion:
                b.companion.extend(m.val)
            elif type(m) == EnvVar:
                b.env_args[m.var] = m.val
            elif type(m) == OCamlRunParam:
                existing = b.env_args.get("OCAMLRUNPARAM")
                if existing:
                    b.env_args["OCAMLRUNPARAM"] = "{},{}".format(existing, m.val)
                else:
                    b.env_args["OCAMLRUNPARAM"] = m.val
            elif type(m) == PerfAndOllyAttach:
                b.perf_and_olly_attach = m
            elif type(m) == MemtraceAttach:
                b.memtrace_attach = m
            elif type(m) == ModifierSet:
                logging.warning("ModifierSet should have been flattened")
        # Per-benchmark OCAMLRUNPARAM override: merge over the modifier-built value,
        # this benchmark's keys winning. Config keys the benchmark doesn't set are
        # kept; keys it sets are replaced (last-value-wins matches OCaml's own
        # OCAMLRUNPARAM parsing). See self.ocamlrunparam in __init__.
        if self.ocamlrunparam:
            merged: Dict[str, Optional[str]] = {}
            for src in (b.env_args.get("OCAMLRUNPARAM", ""), self.ocamlrunparam):
                for tok in src.split(","):
                    tok = tok.strip()
                    if not tok:
                        continue
                    key, sep, val = tok.partition("=")
                    merged[key] = val if sep else None
            b.env_args["OCAMLRUNPARAM"] = ",".join(
                k if v is None else "{}={}".format(k, v) for k, v in merged.items())
        return b

    def to_string(self, runtime: Runtime) -> str:
        return "{} {}".format(
            self.get_env_str(),
            " ".join([
                smart_quote(os.path.expandvars(x))
                for x in self.get_full_args(runtime)
            ])
        )

    def _run_with_perf_and_olly(
        self,
        cmd: List,
        env_args: Dict[str, str],
        cwd: Optional[Path],
        modifier: 'PerfAndOllyAttach',
    ) -> Tuple[bytes, bytes, SubprocessrExit]:
        """Run benchmark with perf stat and olly gc-stats attached via a sync pipe.

        Owns the per-invocation tmpdir holding the OCaml runtime_events ring
        and intermediate perf/olly output. Always removes tmpdir before
        returning — GC-heavy benchmarks (owl_gc, liq_video_frames) leave 100+
        MB ring files behind, and over a multi-thousand-invocation run those
        will fill the tmpfs and the next benchmark dies with SIGBUS (mmap
        write fails on the ring buffer).
        """
        tmpdir = tempfile.mkdtemp(prefix="running-ng-events-")
        # Warn early if the tmpfs is already low so the run can be aborted
        # before we start producing SIGBUS-killed cells.
        try:
            free_mb = shutil.disk_usage(tmpdir).free // (1024 * 1024)
            if free_mb < 1024:
                logging.warning(
                    "Only %d MiB free on tmpdir filesystem (%s); runtime_events ring may "
                    "fail with SIGBUS. Free space before continuing.", free_mb, tmpdir)
        except OSError:
            pass
        try:
            return self._run_with_perf_and_olly_in_tmpdir(
                tmpdir, cmd, env_args, cwd, modifier)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _run_with_perf_and_olly_in_tmpdir(
        self,
        tmpdir: str,
        cmd: List,
        env_args: Dict[str, str],
        cwd: Optional[Path],
        modifier: 'PerfAndOllyAttach',
    ) -> Tuple[bytes, bytes, SubprocessrExit]:
        """Body of _run_with_perf_and_olly that uses an externally-owned tmpdir.

        Using SIGSTOP in preexec_fn deadlocks because Python's Popen blocks
        reading the internal errpipe until the child calls exec() (which closes
        the CLOEXEC fd). Instead, we block the child in preexec_fn by reading
        from a pipe: parent gets the pid, starts observers, then closes the write
        end so the child unblocks and proceeds to exec().
        """
        env_args = env_args.copy()
        env_args["OCAML_RUNTIME_EVENTS_START"] = "1"
        env_args["OCAML_RUNTIME_EVENTS_DIR"] = tmpdir
        env_args["OCAML_RUNTIME_EVENTS_PRESERVE"] = "1"

        # Spawn a tiny Python wrapper as the child.  It blocks reading from
        # sync_r, then os.execvp's into the real benchmark.  The PID is
        # preserved through exec(), so perf and olly track it correctly.
        #
        # We cannot use preexec_fn for blocking because Popen.__init__ itself
        # blocks reading its internal errpipe until the child calls exec().
        # Any blocking inside preexec_fn therefore deadlocks Popen.
        sync_r, sync_w = os.pipe()
        sync_env = env_args.copy()
        sync_env["_BENCH_SYNC_FD"] = str(sync_r)

        # sys.argv in -c mode: ['-c', cmd[0], cmd[1], ...]
        wrapper = (
            "import os,sys; "
            "fd=int(os.environ.pop('_BENCH_SYNC_FD')); "
            "os.read(fd,1); os.close(fd); "
            "os.execvp(sys.argv[1], sys.argv[1:])"
        )
        bench = subprocess.Popen(
            ["python3", "-c", wrapper] + [str(c) for c in cmd],
            env=sync_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            pass_fds=(sync_r,),
            cwd=cwd,
        )
        pid = bench.pid
        os.close(sync_r)

        # Attach perf to the PID while the wrapper is blocked — perf follows exec()
        perf_output = os.path.join(tmpdir, "perf.json")
        perf_cmd = ["perf", "stat", "--json", "--inherit", "-p", str(pid), "-o", perf_output]
        if modifier.perf_events:
            perf_cmd.extend(["-e", ",".join(modifier.perf_events)])
        perf_p, perf_ctl_fd, perf_ack_fd = _start_perf_armed(perf_cmd, tmpdir, "main")

        # Whole-process CPU time and fault counts, straight from the kernel.
        # RUSAGE_CHILDREN only accounts for children already reaped, and nothing
        # is reaped between here and the post-run snapshot (perf and olly are
        # waited for later), so the delta is exactly this benchmark's usage.
        # This is ground truth for utime/stime/faults independent of perf, and
        # the cross-check that catches a perf attach that missed threads.
        ru_before = resource.getrusage(resource.RUSAGE_CHILDREN)

        # Release: wrapper execs the benchmark, OCaml runtime starts, ring buffer is created
        os.close(sync_w)

        # Wait for a *.events file in tmpdir whose PID belongs to the real
        # benchmark (not a short-lived helper).  Some benchmark wrapper
        # scripts run OCaml programs in `$(...)` subshells before exec'ing
        # the real benchmark — e.g. coq's wrapper runs `ocamlfind printconf
        # stdlib` to set up OCAMLPATH.  With OCAML_RUNTIME_EVENTS_START=1
        # inherited from our env, each of those writes a .events file with
        # its own short-lived PID.  If we latch onto one of those we'll
        # miss the real benchmark.
        #
        # Filter strategy:
        #   1. PID must still be alive (kill(pid, 0) succeeds), AND
        #   2. The process exe path must not look like a build/setup tool
        #      (ocamlfind, ocamlc, dune, ocamlopt ...).
        # Tool-filter runs off /proc/<pid>/exe on Linux; on other platforms
        # only the alive check applies.
        # Wait for a usable *.events file in tmpdir.  Priority:
        #
        #   1. The wrapper PID's own events file.  Our Python wrapper
        #      execvp's the benchmark script, and the script ultimately
        #      exec's the real benchmark binary — so `/proc/<wrapper_pid>`
        #      ends up as the benchmark itself with the same PID.  Any
        #      other .events files in tmpdir come from short-lived OCaml
        #      helpers that the wrapper script invoked in $(...)
        #      subshells (e.g. coq's wrapper runs `ocamlfind printconf
        #      stdlib` to set up OCAMLPATH).
        #
        #   2. Fall back to scanning for other alive, non-build-tool PIDs
        #      — needed when a wrapper like /usr/bin/time forks a child
        #      with a different PID than the one we launched.
        BUILD_TOOLS = {"ocamlfind", "ocamlc", "ocamlc.opt", "ocamlopt",
                       "ocamlopt.opt", "ocaml", "ocamldep", "ocamlmklib",
                       "ocamllex", "ocamlyacc", "dune", "menhir",
                       "bash", "sh"}

        def pid_alive(p: int) -> bool:
            try:
                os.kill(p, 0)
                return True
            except OSError:
                return False

        def pid_is_benchmark(p: int) -> bool:
            """PID must be alive and its exe must not be a known setup tool.

            If we can't read /proc/<pid>/exe (zombie, transient, permission
            denied …) we reject rather than accept — defaulting to accept
            lets dying subshells slip through when their readlink briefly
            fails.
            """
            if not pid_alive(p):
                return False
            try:
                exe = os.path.basename(os.readlink("/proc/{}/exe".format(p)))
            except OSError:
                return False
            return exe not in BUILD_TOOLS

        events_file = None
        ocaml_pid = None
        bench_exited_early = False
        wrapper_events = os.path.join(tmpdir, "{}.events".format(pid))
        deadline = time.time() + 10.0
        while time.time() < deadline:
            # Prefer the wrapper's own events file whenever it exists and
            # the PID has exec'd into the benchmark binary.
            if os.path.exists(wrapper_events) and pid_is_benchmark(pid):
                events_file = wrapper_events
                ocaml_pid = pid
                break
            # Fallback: any other alive, non-build-tool PID.
            hits = sorted(glob.glob(os.path.join(tmpdir, "*.events")))
            candidates = [h for h in hits
                          if pid_is_benchmark(int(os.path.basename(h)[:-len(".events")]))]
            if candidates:
                events_file = max(candidates, key=os.path.getmtime)
                ocaml_pid = int(os.path.basename(events_file)[:-len(".events")])
                break
            # A benchmark that already exited (crashed on startup, or finished
            # in milliseconds) can never become attachable — a dead PID fails
            # pid_is_benchmark forever, so waiting out the deadline is 10s of
            # pure stall per invocation (times each config). poll() also reaps
            # the zombie, which kill(pid, 0) would otherwise keep "alive".
            if bench.poll() is not None:
                bench_exited_early = True
                break
            time.sleep(0.01)

        if events_file is None:
            if bench_exited_early:
                logging.warning(
                    "benchmark exited (code %s) before olly could attach — too "
                    "short to measure; no olly data for this invocation",
                    bench.returncode)
            else:
                logging.warning("No runtime events file found in %s; olly will not attach", tmpdir)
            olly_p = None
        else:
            # If the actual OCaml PID differs from bench.pid (e.g. /usr/bin/time
            # forked a child), re-attach perf to the real benchmark PID so that
            # hardware counters track the OCaml process, not the idle wrapper.
            if ocaml_pid != pid:
                logging.info("OCaml PID %d differs from wrapper PID %d; re-attaching perf", ocaml_pid, pid)
                perf_p.kill()
                perf_p.wait()
                for _fd in (perf_ctl_fd, perf_ack_fd):
                    if _fd is not None:
                        os.close(_fd)
                perf_cmd_new = ["perf", "stat", "--json", "--inherit", "-p", str(ocaml_pid), "-o", perf_output]
                if modifier.perf_events:
                    perf_cmd_new.extend(["-e", ",".join(modifier.perf_events)])
                perf_p, perf_ctl_fd, perf_ack_fd = _start_perf_armed(perf_cmd_new, tmpdir, "reattach")

            # olly writes its JSON output to stderr by default.  That gets
            # interleaved with ring-buffer warnings like "[ring_id=0] Lost N
            # events" on GC-heavy workloads, corrupting the JSON.  Use
            # --output to redirect JSON to a file and leave stderr for the
            # warnings we want to discard.
            olly_output = os.path.join(tmpdir, "olly.json")
            olly_p = subprocess.Popen(
                ["olly", "gc-stats", "--json", "--output", olly_output,
                 "--attach", "{}:{}".format(tmpdir, ocaml_pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

        try:
            _, bench_stderr = bench.communicate(timeout=self.timeout)
            # A crash (e.g. SIGSEGV) returns non-zero but raises no exception, so
            # this must inspect returncode explicitly — otherwise a crashed run is
            # reported Normal and its partial olly/perf output pollutes results.
            # `_exit_is_expected` is what keeps a benchmark whose non-zero exit is
            # the workload (expected_exit:) from being mistaken for such a crash.
            subprocess_exit = (SubprocessrExit.Normal
                               if self._exit_is_expected(bench.returncode)
                               else SubprocessrExit.Error)
        except subprocess.TimeoutExpired:
            bench.kill()
            # If the actual OCaml process is a forked child of the wrapper
            # (e.g. /usr/bin/time), killing the wrapper leaves the child
            # reparented to init, still holding the stderr pipe open.
            # Kill it explicitly to avoid an infinite hang on communicate().
            if ocaml_pid is not None and ocaml_pid != pid:
                try:
                    os.kill(ocaml_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            _, bench_stderr = bench.communicate(timeout=30)
            subprocess_exit = SubprocessrExit.Timeout

        # Snapshot before perf/olly are reaped so their usage stays out of the delta.
        ru_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        rusage = {
            "user_time": round(ru_after.ru_utime - ru_before.ru_utime, 3),
            "system_time": round(ru_after.ru_stime - ru_before.ru_stime, 3),
            "minor_faults": ru_after.ru_minflt - ru_before.ru_minflt,
            "major_faults": ru_after.ru_majflt - ru_before.ru_majflt,
            "voluntary_ctx_switches": ru_after.ru_nvcsw - ru_before.ru_nvcsw,
            "involuntary_ctx_switches": ru_after.ru_nivcsw - ru_before.ru_nivcsw,
        }

        for _fd in (perf_ctl_fd, perf_ack_fd):
            if _fd is not None:
                os.close(_fd)

        # perf stat exits automatically when its target exits
        try:
            perf_p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            perf_p.kill()
            perf_p.wait()

        # --- Build structured JSON result ---
        structured: Dict[str, Any] = {"rusage": rusage}

        # olly gc-stats --json output (written to olly_output via --output)
        if olly_p is not None:
            try:
                _, olly_stderr = olly_p.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                logging.warning("olly gc-stats did not exit after 30 seconds. Killing.")
                olly_p.kill()
                _, olly_stderr = olly_p.communicate()
            if olly_stderr:
                # Lost-events warnings etc. — surface the first line, don't spam.
                lines = olly_stderr.decode("utf-8", errors="replace").splitlines()
                if lines:
                    logging.info("olly stderr: %s (and %d more lines)", lines[0],
                                 max(0, len(lines) - 1))
            try:
                with open(olly_output, "r") as f:
                    olly_text = f.read()
            except FileNotFoundError:
                olly_text = ""
                logging.warning("olly output file %s not found", olly_output)
            # olly emits C-style `-nan` / `nan` / `-inf` / `inf` when a metric
            # is computed from zero events (e.g. gc_overhead = 0/0).  These
            # aren't valid JSON, so substitute `null` before parsing.
            olly_text_clean = re.sub(
                r"(?<![A-Za-z0-9_])-?(?:nan|inf(?:inity)?)\b",
                "null",
                olly_text,
                flags=re.IGNORECASE,
            )
            try:
                structured["olly"] = json.loads(olly_text_clean)
            except (json.JSONDecodeError, ValueError) as e:
                logging.warning("Failed to parse olly JSON output: %s", e)
                structured["olly_raw"] = olly_text

        # perf stat --json output (NDJSON: one JSON object per counter)
        try:
            with open(perf_output, "r") as f:
                perf_lines = []
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            perf_lines.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
                structured["perf"] = perf_lines
        except FileNotFoundError:
            logging.warning("perf output file %s not found", perf_output)

        # Cross-check perf against the kernel's own accounting. task-clock is
        # CPU time over all threads, so it should match utime+stime closely; a
        # large shortfall means perf counted only some threads and every counter
        # in this invocation is an under-report.
        cpu_time = rusage["user_time"] + rusage["system_time"]
        task_clock_s = None
        for entry in structured.get("perf", []):
            if entry.get("event") == "task-clock":
                try:
                    task_clock_s = float(entry["counter-value"]) / 1e9
                except (KeyError, TypeError, ValueError):
                    pass
        if task_clock_s is not None and cpu_time > 1.0 and task_clock_s < 0.8 * cpu_time:
            structured["perf_incomplete"] = True
            logging.warning(
                "perf task-clock (%.1fs) is well below the kernel's CPU time for %s "
                "(%.1fs user+sys): perf missed threads, so its counters under-report "
                "this invocation. Use the rusage block instead.",
                task_clock_s, self.name, cpu_time)

        companion_out = json.dumps(structured, indent=2).encode("utf-8")
        return bench_stderr if bench_stderr else b"", companion_out, subprocess_exit

    def run(self, runtime: Runtime, cwd: Optional[Path] = None, memtrace_path: Optional[Path] = None) -> Tuple[bytes, bytes, SubprocessrExit]:
        if suite.is_dry_run():
            print(
                self.to_string(runtime),
                file=sys.stderr
            )
            return b"", b"", SubprocessrExit.Dryrun
        else:
            cmd = list(runtime.get_command_prefix()) + self.get_full_args(runtime)
            cmd = [os.path.expandvars(x) for x in cmd]
            env_args = os.environ.copy()
            env_args.update(self.env_args)
            effective_cwd = self.override_cwd if self.override_cwd else cwd

            if self.memtrace_attach is not None and memtrace_path is not None:
                env_args["MEMTRACE"] = str(memtrace_path)
                if self.memtrace_attach.rate:
                    env_args["MEMTRACE_RATE"] = str(self.memtrace_attach.rate)

            if self.perf_and_olly_attach is not None:
                return self._run_with_perf_and_olly(cmd, env_args, effective_cwd, self.perf_and_olly_attach)

            companion_out = b""
            stdout: Optional[bytes]
            if self.companion:
                companion_p = subprocess.Popen(
                    self.companion, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                sleep(COMPANION_WAIT_START)
            try:
                p = subprocess.run(
                    cmd,
                    env=env_args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout,
                    cwd=effective_cwd,
                )
                # subprocess.run without check=True does not raise on a non-zero
                # exit, so a crash returns here — inspect returncode explicitly.
                # See `_exit_is_expected` for why this is not just `== 0`.
                subprocess_exit = (SubprocessrExit.Normal
                                   if self._exit_is_expected(p.returncode)
                                   else SubprocessrExit.Error)
                stdout = p.stderr
            except subprocess.CalledProcessError as e:
                subprocess_exit = SubprocessrExit.Error
                stdout = e.stderr
            except subprocess.TimeoutExpired as e:
                subprocess_exit = SubprocessrExit.Timeout
                stdout = e.stderr
            finally:
                if self.companion:
                    try:
                        companion_stdout, _ = companion_p.communicate(
                            timeout=10)
                        companion_out += companion_stdout
                    except subprocess.TimeoutExpired:
                        logging.warning(
                            "Companion program not exited after 10 seconds timeout. Trying to kill ...")
                        try:
                            companion_p.kill()
                        except PermissionError:
                            logging.warning("Failed to kill.")
                        companion_stdout, _ = companion_p.communicate()
                        companion_out += companion_stdout

            return stdout if stdout else b"", companion_out, subprocess_exit


class BinaryBenchmark(Benchmark):
    def __init__(self, program: Path, program_args: List[Union[str, Path]], **kwargs):
        super().__init__(**kwargs)
        self.program = program
        self.program_args = program_args
        assert program.exists()

    def __str__(self) -> str:
        return self.to_string(DummyRuntime(""))

    def attach_modifiers(self, modifiers: List[Modifier]) -> 'BinaryBenchmark':
        bb = super().attach_modifiers(modifiers)
        for m in modifiers:
            if self.suite_name in m.excludes:
                if self.name in m.excludes[self.suite_name]:
                    continue
            elif type(m) == ProgramArg:
                bb.program_args.extend(m.val)
            elif type(m) == JVMArg:
                logging.warning("JVMArg not respected by BinaryBenchmark")
            elif isinstance(m, JVMClasspathAppend) or type(m) == JVMClasspathPrepend:
                logging.warning(
                    "JVMClasspath not respected by BinaryBenchmark")
            elif type(m) == JSArg:
                logging.warning(
                    "JSArg not respected by BinaryBenchmark")
        return bb

    def get_full_args(self, _runtime: Runtime) -> List[Union[str, Path]]:
        cmd = super().get_full_args(_runtime)
        cmd.append(self.program)
        cmd.extend(self.program_args)
        return cmd


class JavaBenchmark(Benchmark):
    def __init__(self, jvm_args: List[str], program_args: List[str], cp: List[str], **kwargs):
        super().__init__(**kwargs)
        self.jvm_args = jvm_args
        self.program_args = program_args
        self.cp = cp

    def get_classpath_args(self) -> List[str]:
        return ["-cp", ":".join(self.cp)] if self.cp else []

    def __str__(self) -> str:
        return self.to_string(DummyRuntime("java"))

    def attach_modifiers(self, modifiers: List[Modifier]) -> 'JavaBenchmark':
        jb = super().attach_modifiers(modifiers)
        for m in modifiers:
            if self.suite_name in m.excludes:
                if self.name in m.excludes[self.suite_name]:
                    continue
            if type(m) == JVMArg:
                jb.jvm_args.extend(m.val)
            elif type(m) == ProgramArg:
                jb.program_args.extend(m.val)
            elif isinstance(m, JVMClasspathAppend):
                jb.cp.extend(m.val)
            elif type(m) == JVMClasspathPrepend:
                jb.cp = m.val + jb.cp
            elif type(m) == JSArg:
                logging.warning(
                    "JSArg not respected by JavaBenchmark")
        return jb

    def get_full_args(self, runtime: Runtime) -> List[Union[str, Path]]:
        cmd = super().get_full_args(runtime)
        cmd.append(runtime.get_executable())
        cmd.extend(self.jvm_args)
        if isinstance(runtime, OpenJDK):
            if runtime.release >= 9:
                cmd.extend([
                    "--add-exports",
                    "java.base/jdk.internal.ref=ALL-UNNAMED"
                ])
        cmd.extend(self.get_classpath_args())
        cmd.extend(self.program_args)
        return cmd


class JavaScriptBenchmark(Benchmark):
    def __init__(self, js_args: List[str], program: str, program_args: List[str], **kwargs):
        super().__init__(**kwargs)
        self.js_args = js_args
        self.program = program
        self.program_args = program_args

    def __str__(self) -> str:
        return self.to_string(DummyRuntime("js"))

    def attach_modifiers(self, modifiers: List[Modifier]) -> 'JavaScriptBenchmark':
        jb = super().attach_modifiers(modifiers)
        for m in modifiers:
            if self.suite_name in m.excludes:
                if self.name in m.excludes[self.suite_name]:
                    continue
            if type(m) == ProgramArg:
                jb.program_args.extend(m.val)
            elif type(m) == JVMArg:
                logging.warning("JVMArg not respected by JavaScriptBenchmark")
            elif isinstance(m, JVMClasspathAppend) or type(m) == JVMClasspathPrepend:
                logging.warning(
                    "JVMClasspath not respected by JavaScriptBenchmark")
            elif type(m) == JSArg:
                jb.js_args.extend(m.val)
        return jb

    def get_full_args(self, runtime: Runtime) -> List[Union[str, Path]]:
        cmd = super().get_full_args(runtime)
        cmd.append(runtime.get_executable())
        cmd.extend(self.js_args)
        cmd.append(self.program)
        if isinstance(runtime, D8):
            cmd.append("--")
        elif isinstance(runtime, JavaScriptCore):
            cmd.append("--")
        elif isinstance(runtime, SpiderMonkey):
            pass
        else:
            raise TypeError("{} is of type {}, and not a valid runtime for JavaScriptBenchmark".format(
                runtime, type(runtime)))
        cmd.extend(self.program_args)
        return cmd


class OCamlBenchmark(Benchmark):
    def __init__(self, ocaml_args: List[str], program: str, program_args: List[str], **kwargs):
        super().__init__(**kwargs)
        self.ocaml_args = ocaml_args
        self.program = program
        self.program_args = program_args

    def __str__(self) -> str:
        return self.to_string(DummyRuntime("ocaml"))

    def attach_modifiers(self, modifiers: List[Modifier]) -> 'OCamlBenchmark':
        ob = super().attach_modifiers(modifiers)
        for m in modifiers:
            if self.suite_name in m.excludes:
                if self.name in m.excludes[self.suite_name]:
                    continue
            if type(m) == ProgramArg:
                ob.program_args.extend(m.val)
            elif type(m) == JVMArg:
                logging.warning("JVMArg not respected by OCamlBenchmark")
            elif isinstance(m, JVMClasspathAppend) or type(m) == JVMClasspathPrepend:
                logging.warning("JVMClasspath not respected by OCamlBenchmark")
            elif type(m) == JSArg:
                logging.warning("JSArg not respected by OCamlBenchmark")
            elif type(m) == OCamlArg:
                ob.ocaml_args.extend(m.val)
        return ob

    def get_full_args(self, runtime: Runtime) -> List[Union[str, Path]]:
        if not isinstance(runtime, OCaml):
            raise TypeError("{} is of type {}, and not a valid runtime for OCamlBenchmark".format(
                runtime, type(runtime)))
        cmd = super().get_full_args(runtime)
        cmd.append(runtime.get_executable())
        cmd.extend(self.ocaml_args)
        cmd.append(self.program)
        cmd.extend(self.program_args)
        return cmd


class OCamlBuiltBinaryBenchmark(Benchmark):
    def __init__(
        self,
        benchmark_name: str,
        benchmark_dir: Path,
        build_script: Optional[Path],
        binary: Optional[str],
        program_args: List[str],
        build_args: List[str],
        build_env: Dict[str, str],
        always_build: bool,
        min_ocaml_major: Optional[int] = None,
        required_runtime_hint: Optional[str] = None,
        isolated_switch: bool = False,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.benchmark_name = benchmark_name
        self.benchmark_dir = benchmark_dir
        self.build_script = build_script
        self.binary = binary
        self.program_args = program_args
        self.build_args = build_args
        self.build_env = build_env
        self.always_build = always_build
        self.min_ocaml_major = min_ocaml_major
        self.required_runtime_hint = required_runtime_hint
        self.isolated_switch: bool = isolated_switch
        self._binary_cache: Dict[str, Path] = {}

    def __str__(self) -> str:
        return self.to_string(DummyRuntime("ocaml-built"))

    def attach_modifiers(self, modifiers: List[Modifier]) -> 'OCamlBuiltBinaryBenchmark':
        ob = super().attach_modifiers(modifiers)
        for m in modifiers:
            if self.suite_name in m.excludes:
                if self.name in m.excludes[self.suite_name]:
                    continue
            if type(m) == ProgramArg:
                ob.program_args.extend(m.val)
            elif type(m) == JVMArg:
                logging.warning("JVMArg not respected by OCamlBuiltBinaryBenchmark")
            elif isinstance(m, JVMClasspathAppend) or type(m) == JVMClasspathPrepend:
                logging.warning("JVMClasspath not respected by OCamlBuiltBinaryBenchmark")
            elif type(m) == JSArg:
                logging.warning("JSArg not respected by OCamlBuiltBinaryBenchmark")
            elif type(m) == OCamlArg:
                logging.warning("OCamlArg not respected by OCamlBuiltBinaryBenchmark")
        return ob

    def _resolve_build_script(self) -> Path:
        if self.build_script:
            return self.build_script.resolve()
        return (self.benchmark_dir / "{}.build.sh".format(self.benchmark_name)).resolve()

    def _resolve_output_binary(self, runtime: OCaml) -> Path:
        if self.binary:
            raw_binary = self.binary.format(
                benchmark=self.benchmark_name,
                runtime=runtime.name
            )
        else:
            raw_binary = "{}-{}".format(self.benchmark_name, runtime.name)
        declared = Path(raw_binary)
        if declared.is_absolute():
            return declared.resolve()
        return (self.benchmark_dir / declared).resolve()

    def _run_build(self, runtime: OCaml, out_binary: Path):
        if suite.is_dry_run():
            return
        if out_binary.exists() and not self.always_build:
            logging.warning(
                "OCaml binary %s already exists; skipping build. Set `always_build: true` to rebuild.",
                out_binary
            )
            return
        out_binary.parent.mkdir(parents=True, exist_ok=True)

        # When isolated_switch is set (macrobenchmarks), create a
        # per-benchmark satellite switch so that opam installs don't
        # pollute the shared runtime switch.
        if self.isolated_switch:
            env = runtime.get_benchmark_switch_env(self.benchmark_name)
            switch_name = runtime.get_benchmark_switch_name(self.benchmark_name)
        else:
            env = runtime.get_switch_env()
            switch_name = runtime.get_switch_name()

        env.update(self.build_env)
        # Runtime build-env overrides (e.g. OCamlMMTk's fixed MMTk heap +
        # libmmtk_ocaml.a on LIBRARY_PATH).  Applied last; the runtime folds in
        # any existing value it wants to preserve.
        env.update(runtime.get_build_env_overrides())
        env["RUNNING_OCAML_OUTPUT"] = str(out_binary)
        env["RUNNING_OCAML_BENCH_DIR"] = str(self.benchmark_dir)
        env["RUNNING_OCAML_RUNTIME_NAME"] = runtime.name
        if switch_name:
            env["RUNNING_OCAML_SWITCH"] = switch_name

        build_script = self._resolve_build_script()
        if not build_script.exists():
            raise RuntimeError("Build script not found at {}".format(build_script))
        cmd: List[str]
        if build_script.suffix == ".sh":
            cmd = ["bash", str(build_script)]
        else:
            cmd = [str(build_script)]
        cmd.extend(self.build_args)
        # Runtime-specific launcher prefix (e.g. OCamlMMTk needs `setarch -R`
        # so the MMTk compiler doesn't flake while building the benchmark).
        cmd = list(runtime.get_command_prefix()) + cmd
        logging.info("Building OCaml benchmark %s with command: %s", self.name, " ".join(cmd))
        subprocess.run(cmd, cwd=str(self.benchmark_dir), env=env, check=True)

        if out_binary.exists():
            return

        raise RuntimeError(
            "Build script completed but binary not found. "
            "Expected RUNNING_OCAML_OUTPUT ({})".format(out_binary)
        )

    def _ensure_binary(self, runtime: OCaml) -> Path:
        if self.min_ocaml_major is not None:
            major = runtime.get_major_version()
            if major < self.min_ocaml_major:
                raise ValueError(
                    "Benchmark {!r} requires OCaml >= {}.x, but runtime {!r} is OCaml {}.x. "
                    "Use an OCaml 5+ runtime for multicore benchmarks.".format(
                        self.name, self.min_ocaml_major, runtime.name, major
                    )
                )
        if self.required_runtime_hint is not None and not isinstance(runtime, OxCaml):
            raise ValueError(
                "Benchmark {!r} requires {}, but runtime {!r} is not an OxCaml runtime. "
                "Use a 'type: OxCaml' runtime in your config.".format(
                    self.name, self.required_runtime_hint, runtime.name
                )
            )
        runtime_key = runtime.get_cache_key()
        cached = self._binary_cache.get(runtime_key)
        if cached and cached.exists() and not self.always_build:
            return cached
        out_binary = self._resolve_output_binary(runtime)
        sentinel = Path(str(out_binary) + ".build-failed")
        if sentinel.exists() and not self.always_build:
            raise RuntimeError(
                "Build previously failed for {} (sentinel: {}). "
                "Delete the sentinel file to retry.".format(out_binary.name, sentinel)
            )
        if suite.is_dry_run():
            self._binary_cache[runtime_key] = out_binary
            return out_binary
        try:
            self._run_build(runtime, out_binary)
        except Exception:
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.touch()
            raise
        sentinel.unlink(missing_ok=True)
        if not out_binary.exists():
            raise RuntimeError("Output binary {} does not exist after build step".format(out_binary))
        self._binary_cache[runtime_key] = out_binary
        return out_binary

    def get_full_args(self, runtime: Runtime) -> List[Union[str, Path]]:
        if not isinstance(runtime, OCaml):
            raise TypeError("{} is of type {}, and not a valid runtime for OCamlBuiltBinaryBenchmark".format(
                runtime, type(runtime)))
        cmd = super().get_full_args(runtime)
        cmd.append(self._ensure_binary(runtime))
        cmd.extend(self.program_args)
        return cmd

    def prepare(self, runtime: Runtime):
        if not isinstance(runtime, OCaml):
            return
        self._ensure_binary(runtime)
