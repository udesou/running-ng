import errno
import glob
import json
import logging
import re
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


class SubprocessrExit(Enum):
    Normal = 1
    Error = 2
    Timeout = 3
    Dryrun = 4


class Benchmark(object):
    def __init__(self, suite_name: str, name: str, wrapper: Optional[str] = None, timeout: Optional[int] = None, override_cwd: Optional[Path] = None, companion: Optional[str] = None, **kwargs):
        self.name = name
        self.suite_name = suite_name
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
        self.timeout = timeout
        # ignore the current working directory provided by commands like runbms or minheap
        # certain benchmarks expect to be invoked from certain directories
        self.override_cwd = override_cwd

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
            elif type(m) == ModifierSet:
                logging.warning("ModifierSet should have been flattened")
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

        Using SIGSTOP in preexec_fn deadlocks because Python's Popen blocks
        reading the internal errpipe until the child calls exec() (which closes
        the CLOEXEC fd). Instead, we block the child in preexec_fn by reading
        from a pipe: parent gets the pid, starts observers, then closes the write
        end so the child unblocks and proceeds to exec().
        """
        tmpdir = tempfile.mkdtemp()
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
        perf_p = subprocess.Popen(perf_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        # Release: wrapper execs the benchmark, OCaml runtime starts, ring buffer is created
        os.close(sync_w)

        # Wait for a *.events file in tmpdir whose PID is still alive.  We
        # scan rather than looking for a specific PID because wrappers like
        # /usr/bin/time fork a new child, so the OCaml process PID differs
        # from bench.pid.
        #
        # Why we filter by "still alive": some benchmark wrapper scripts run
        # short-lived OCaml programs (e.g. `ocamlfind printconf stdlib` in a
        # `$(...)` subshell) *before* exec'ing the real benchmark.  With
        # OCAML_RUNTIME_EVENTS_START=1 inherited from our env, each of those
        # writes a .events file with its own short-lived PID, and without
        # this check we'd latch onto that dead PID and miss the real
        # benchmark entirely.
        def pid_alive(p: int) -> bool:
            try:
                os.kill(p, 0)
                return True
            except OSError:
                return False

        events_file = None
        ocaml_pid = None
        deadline = time.time() + 10.0
        while time.time() < deadline:
            hits = sorted(glob.glob(os.path.join(tmpdir, "*.events")))
            # Prefer any file whose PID is still alive; ignore dead ones.
            alive = [h for h in hits
                     if pid_alive(int(os.path.basename(h)[:-len(".events")]))]
            if alive:
                # If multiple are alive (rare), prefer the latest by mtime —
                # the final exec'd process usually writes last.
                events_file = max(alive, key=os.path.getmtime)
                ocaml_pid = int(os.path.basename(events_file)[:-len(".events")])
                break
            time.sleep(0.01)

        if events_file is None:
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
                perf_cmd_new = ["perf", "stat", "--json", "--inherit", "-p", str(ocaml_pid), "-o", perf_output]
                if modifier.perf_events:
                    perf_cmd_new.extend(["-e", ",".join(modifier.perf_events)])
                perf_p = subprocess.Popen(perf_cmd_new, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

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
            subprocess_exit = SubprocessrExit.Normal
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

        # perf stat exits automatically when its target exits
        try:
            perf_p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            perf_p.kill()
            perf_p.wait()

        # --- Build structured JSON result ---
        structured: Dict[str, Any] = {}

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

        companion_out = json.dumps(structured, indent=2).encode("utf-8")
        return bench_stderr if bench_stderr else b"", companion_out, subprocess_exit

    def run(self, runtime: Runtime, cwd: Optional[Path] = None) -> Tuple[bytes, bytes, SubprocessrExit]:
        if suite.is_dry_run():
            print(
                self.to_string(runtime),
                file=sys.stderr
            )
            return b"", b"", SubprocessrExit.Dryrun
        else:
            cmd = self.get_full_args(runtime)
            cmd = [os.path.expandvars(x) for x in cmd]
            env_args = os.environ.copy()
            env_args.update(self.env_args)
            effective_cwd = self.override_cwd if self.override_cwd else cwd

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
                subprocess_exit = SubprocessrExit.Normal
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
