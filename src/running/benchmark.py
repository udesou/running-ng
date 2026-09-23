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
from running import osinfo
from running import counters
import os
from enum import Enum
import pty

def _is_dry_run() -> bool:
    """Imported lazily: running.suite imports names from this module, so a
    module-level import here would make the import order matter."""
    from running import suite
    return suite.is_dry_run()


# Never the benchmark itself: wrapper scripts may run these in $(...) subshells
# before exec'ing the real binary, and each then writes its own .events file.
BUILD_TOOLS = {"ocamlfind", "ocamlc", "ocamlc.opt", "ocamlopt",
               "ocamlopt.opt", "ocaml", "ocamldep", "ocamlmklib",
               "ocamllex", "ocamlyacc", "dune", "menhir",
               "bash", "sh"}


#: perf names the unit of each counter-value in a sibling field (task-clock is usually "msec").
_COUNTER_UNIT_SECONDS = {
    "sec": 1.0, "s": 1.0, "msec": 1e-3, "ms": 1e-3,
    "usec": 1e-6, "us": 1e-6, "nsec": 1e-9, "ns": 1e-9,
}


def counter_seconds(entry: Dict[str, Any]) -> Optional[float]:
    """A time-valued perf counter in seconds, or None. `counter-value` is in
    the unit named by `unit`; the sibling `event-runtime` is nanoseconds."""
    try:
        value = float(entry["counter-value"])
    except (KeyError, TypeError, ValueError):
        return None
    scale = _COUNTER_UNIT_SECONDS.get((entry.get("unit") or "").strip().lower())
    if scale is not None:
        return value * scale
    # event-runtime is documented as nanoseconds
    try:
        return float(entry["event-runtime"]) / 1e9
    except (KeyError, TypeError, ValueError):
        return None


def _warn_if_gc_stats_unreliable(benchmark_name: str, olly: Any) -> None:
    """Warn when olly reports `stats_reliable: false` (runtime_events ring
    overflowed, GC figures undercount). The fix is a bigger ring (`re-25`).
    """
    if not isinstance(olly, dict):
        return
    if olly.get("stats_reliable") is False:
        lost = olly.get("lost_events") or olly.get("lost_words")
        logging.warning(
            "olly reports stats_reliable=false for %s: the runtime_events ring "
            "overflowed%s, so its GC metrics are an undercount. Add a larger "
            "ring (e.g. `re-25`) to this config. Hardware counters and rusage "
            "in this record are unaffected.",
            benchmark_name,
            " (lost {})".format(lost) if lost else "")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def pid_is_benchmark(pid: int) -> bool:
    """PID is alive and its exe is not a known setup tool.

    A failed exe lookup rejects (dying subshells must not slip through); a
    platform with no lookup at all applies only the alive check.
    """
    if not pid_alive(pid):
        return False
    exe = osinfo.pid_exe_name(pid)
    if exe is None:
        return not osinfo.EXE_LOOKUP_SUPPORTED
    return exe not in BUILD_TOOLS


COMPANION_WAIT_START = 2.0

class SubprocessrExit(Enum):
    Normal = 1
    Error = 2
    Timeout = 3
    Dryrun = 4


class Benchmark(object):
    def __init__(self, suite_name: str, name: str, wrapper: Optional[str] = None, timeout: Optional[int] = None, override_cwd: Optional[Path] = None, companion: Optional[str] = None, expected_exit: int = 0, **kwargs):
        self.name = name
        self.suite_name = suite_name
        # Exit code of a successful run; non-zero for some workloads (see _exit_is_expected).
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
        # Set by CpuPin; carries the observer CPU set for olly and the counter tool.
        self.cpu_pin: Optional[CpuPin] = None
        self.memtrace_attach: Optional[MemtraceAttach] = None
        self.timeout = timeout
        # ignore the current working directory provided by commands like runbms or minheap
        # certain benchmarks expect to be invoked from certain directories
        self.override_cwd = override_cwd
        # Per-benchmark OCAMLRUNPARAM from the suite; its keys override the
        # config string's re/md modifiers in attach_modifiers.
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
        return

    def _exit_is_expected(self, returncode: Optional[int]) -> bool:
        """True if `returncode` is None (still running) or equals `expected_exit`.

        Some workloads exit non-zero by design (alt_ergo_unsat_smt2 dies of its
        own SIGVTALRM, 142); the field is spelled as in macro-benches' manifest.yml.
        """
        if returncode is None:
            return True
        return returncode == self.expected_exit

    def in_modifier_scope(self, m: Modifier) -> bool:
        """False when `m` carries an `includes` list this benchmark is not on;
        `excludes` is handled by the callers and still subtracts."""
        if not m.includes:
            return True
        return self.name in m.includes.get(self.suite_name, ())

    def attach_modifiers(self, modifiers: List[Modifier]) -> Any:
        b = deepcopy(self)
        for m in modifiers:
            if not self.in_modifier_scope(m):
                continue
            if self.suite_name in m.excludes:
                if self.name in m.excludes[self.suite_name]:
                    continue
            elif type(m) == Wrapper:
                b.wrapper.extend(m.val)
            elif type(m) == CpuPin:
                # Like a Wrapper, with the command derived from this machine's topology.
                if b.cpu_pin is not None and m.val:
                    # Nested taskset prefixes: the inner one silently wins.
                    logging.warning(
                        "%s: CpuPin %s applies on top of %s; the innermost "
                        "pin wins. Scope them apart with excludes.",
                        self.name, m.name, b.cpu_pin.name)
                b.wrapper.extend(m.val)
                b.cpu_pin = m
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
        # Merge the benchmark's own OCAMLRUNPARAM keys over the modifiers'
        # (last value wins, as in OCaml's own parsing).
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
        """Run with perf stat and olly gc-stats attached.

        Owns the per-invocation tmpdir (runtime_events ring, tool output) and
        always removes it: leftover 100+ MB ring files fill the tmpfs and the
        next benchmark dies with SIGBUS.
        """
        tmpdir = tempfile.mkdtemp(prefix="running-ng-events-")
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
        """Body of _run_with_perf_and_olly with an externally-owned tmpdir."""
        env_args = env_args.copy()
        env_args["OCAML_RUNTIME_EVENTS_START"] = "1"
        env_args["OCAML_RUNTIME_EVENTS_DIR"] = tmpdir
        env_args["OCAML_RUNTIME_EVENTS_PRESERVE"] = "1"

        # The child is a Python trampoline that blocks on sync_r, then execvp's
        # the benchmark (same PID, so the tools track it). Blocking in
        # preexec_fn instead would deadlock: Popen reads its errpipe until exec().
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
        # sys.executable, not "python3": the child's PATH may not resolve one
        # (os.defpath has no python3 on FreeBSD).
        bench = subprocess.Popen(
            [sys.executable, "-c", wrapper] + [str(c) for c in cmd],
            env=sync_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            pass_fds=(sync_r,),
            cwd=cwd,
        )
        pid = bench.pid
        os.close(sync_r)

        # Attach while the trampoline is still blocked; attaching (not
        # launching) keeps the benchmark our direct child.
        backend = counters.select_backend()
        # Keep the observers off the benchmark's CPUs; olly continuously
        # drains the runtime_events ring alongside the benchmark.
        observer_prefix = (osinfo.pin_command(self.cpu_pin.observer_cpus)
                           if self.cpu_pin is not None else [])
        counter_handle = backend.attach(pid, tmpdir, modifier.perf_events, "main",
                                        pin_prefix=observer_prefix)

        # RUSAGE_CHILDREN counts reaped children only; nothing else is reaped
        # before the post-run snapshot, so the delta is this benchmark's usage.
        ru_before = resource.getrusage(resource.RUSAGE_CHILDREN)

        # Releasing before the tool counts loses or under-counts the invocation.
        backend.wait_ready(counter_handle)

        # release the trampoline
        os.close(sync_w)

        # Wait for the real benchmark's *.events file: prefer the trampoline
        # PID's own, else any alive non-build-tool PID (a wrapper like
        # /usr/bin/time forks a child with a different PID). Short-lived
        # helpers in the wrapper script write .events files too.
        events_file = None
        ocaml_pid = None
        bench_exited_early = False
        wrapper_events = os.path.join(tmpdir, "{}.events".format(pid))
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if os.path.exists(wrapper_events) and pid_is_benchmark(pid):
                events_file = wrapper_events
                ocaml_pid = pid
                break
            hits = sorted(glob.glob(os.path.join(tmpdir, "*.events")))
            candidates = [h for h in hits
                          if pid_is_benchmark(int(os.path.basename(h)[:-len(".events")]))]
            if candidates:
                events_file = max(candidates, key=os.path.getmtime)
                ocaml_pid = int(os.path.basename(events_file)[:-len(".events")])
                break
            # An exited benchmark never becomes attachable; do not wait out
            # the deadline. poll() also reaps the zombie kill(pid, 0) sees as alive.
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
            assert ocaml_pid is not None
            # e.g. /usr/bin/time forked a child: track that, not the idle wrapper
            if ocaml_pid != pid:
                logging.info("OCaml PID %d differs from wrapper PID %d; re-attaching %s",
                             ocaml_pid, pid, backend.name)
                backend.kill(counter_handle)
                counter_handle = backend.attach(
                    ocaml_pid, tmpdir, modifier.perf_events, "reattach",
                    pin_prefix=observer_prefix)

            # --output keeps the JSON apart from "[ring_id=0] Lost N events"
            # warnings on stderr, which would corrupt it.
            olly_output = os.path.join(tmpdir, "olly.json")
            olly_p = subprocess.Popen(
                list(observer_prefix) +
                ["olly", "gc-stats", "--json", "--output", olly_output,
                 "--attach", "{}:{}".format(tmpdir, ocaml_pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

        try:
            _, bench_stderr = bench.communicate(timeout=self.timeout)
            # A crash raises no exception here; inspect returncode.
            subprocess_exit = (SubprocessrExit.Normal
                               if self._exit_is_expected(bench.returncode)
                               else SubprocessrExit.Error)
        except subprocess.TimeoutExpired:
            bench.kill()
            # A forked child of the wrapper is reparented to init and keeps
            # the stderr pipe open, hanging communicate().
            if ocaml_pid is not None and ocaml_pid != pid:
                try:
                    os.kill(ocaml_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            _, bench_stderr = bench.communicate(timeout=30)
            subprocess_exit = SubprocessrExit.Timeout

        # before perf/olly are reaped, so their usage stays out of the delta
        ru_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        rusage = {
            "user_time": round(ru_after.ru_utime - ru_before.ru_utime, 3),
            "system_time": round(ru_after.ru_stime - ru_before.ru_stime, 3),
            "minor_faults": ru_after.ru_minflt - ru_before.ru_minflt,
            "major_faults": ru_after.ru_majflt - ru_before.ru_majflt,
            "voluntary_ctx_switches": ru_after.ru_nvcsw - ru_before.ru_nvcsw,
            "involuntary_ctx_switches": ru_after.ru_nivcsw - ru_before.ru_nivcsw,
        }

        backend.stop(counter_handle)

        structured: Dict[str, Any] = {"rusage": rusage}

        if olly_p is not None:
            try:
                _, olly_stderr = olly_p.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                logging.warning("olly gc-stats did not exit after 30 seconds. Killing.")
                olly_p.kill()
                _, olly_stderr = olly_p.communicate()
            if olly_stderr:
                # surface the first line only
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
            # olly emits C-style nan/inf (e.g. gc_overhead = 0/0), which is not JSON.
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
            else:
                _warn_if_gc_stats_unreliable(self.name, structured["olly"])

        # Keyed "perf" whatever the backend, for existing consumers.
        structured["counter_backend"] = backend.name
        if counter_handle is not None:
            structured["perf"] = backend.collect(counter_handle)

        # task-clock should match utime+stime; a large shortfall means perf
        # missed threads and every counter is an under-report. Only
        # linux-perf reports it, so the check is absent on other backends.
        cpu_time = rusage["user_time"] + rusage["system_time"]
        task_clock_s = None
        for entry in structured.get("perf", []):
            if entry.get("event") == "task-clock":
                task_clock_s = counter_seconds(entry)
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
        if _is_dry_run():
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
                # A crash raises no exception here; inspect returncode.
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
            if not self.in_modifier_scope(m):
                continue
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
            if not self.in_modifier_scope(m):
                continue
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
            if not self.in_modifier_scope(m):
                continue
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
        if _is_dry_run():
            return
        if out_binary.exists() and not self.always_build:
            logging.warning(
                "OCaml binary %s already exists; skipping build. Set `always_build: true` to rebuild.",
                out_binary
            )
            return
        out_binary.parent.mkdir(parents=True, exist_ok=True)

        # isolated_switch: opam installs go to a per-benchmark satellite switch.
        if self.isolated_switch:
            env = runtime.get_benchmark_switch_env(self.benchmark_name)
            switch_name = runtime.get_benchmark_switch_name(self.benchmark_name)
        else:
            env = runtime.get_switch_env()
            switch_name = runtime.get_switch_name()

        env.update(self.build_env)
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
        if _is_dry_run():
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
