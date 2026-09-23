from running.modifier import JVMArg, Modifier, JSArg, EnvVar
from typing import Any, Dict, List, Optional, Set, Union
from pathlib import Path
import logging
from running.util import register
import fcntl
import hashlib
import os
import re
import subprocess
import sys
import tempfile


class OpamRootBusyError(RuntimeError):
    """Another running-ng run holds the opam root this run needs to mutate."""


class Runtime(object):
    CLS_MAPPING: Dict[str, Any]
    CLS_MAPPING = {}

    def __init__(self, name: str, **kwargs):
        self.name = name

    @staticmethod
    def from_config(name: str, config: Dict[str, str]) -> Any:
        runtime_type = config.get("type")
        if runtime_type is None:
            if any(k in config for k in ["executable", "version", "commit", "hash"]):
                runtime_type = "OCaml"
            else:
                raise KeyError(
                    "Runtime {} missing `type` and no inferable OCaml keys (executable/version/commit/hash).".format(name)
                )
        return Runtime.CLS_MAPPING[runtime_type](name=name, **config)

    def get_executable(self) -> Union[str, Path]:
        raise NotImplementedError

    def get_heapsize_modifier(self, size: int) -> Modifier:
        raise NotImplementedError

    def is_oom(self, _output: bytes) -> bool:
        raise NotImplementedError

    def get_cache_key(self) -> str:
        return "{}-{}".format(type(self).__name__.lower(), self.name)

    def get_command_prefix(self) -> List[str]:
        """Tokens prepended to every benchmark build and run command (e.g. a setarch wrapper)."""
        return []

    def get_build_env_overrides(self) -> Dict[str, str]:
        """Env-var overrides for the benchmark build environment, applied after the suite's build_env."""
        return {}

class DummyRuntime(Runtime):
    def __init__(self, executable: str):
        super().__init__(name="dummy")
        self.executable = executable

    def get_executable(self) -> Union[str, Path]:
        return self.executable

    def is_oom(self, _output: bytes) -> bool:
        return False


@register(Runtime)
class NativeExecutable(Runtime):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def get_executable(self) -> Union[str, Path]:
        return ""

    def is_oom(self, _output: bytes) -> bool:
        return False


class JVM(Runtime):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def get_executable(self) -> Path:
        raise NotImplementedError

    def __str__(self):
        return "JVM {}".format(self.name)

    def get_heapsize_modifier(self, size: int) -> Modifier:
        size_str = "{}M".format(size)
        heapsize = JVMArg(
            name="heap{}".format(size_str),
            val="-Xms{} -Xmx{}".format(size_str, size_str)
        )
        return heapsize

    def is_oom(self, output: bytes) -> bool:
        for pattern in [b"Allocation Failed", b"OutOfMemoryError", b"ran out of memory", b"panicked at 'Out of memory!'"]:
            if pattern in output:
                return True
        return False


@register(Runtime)
class OpenJDK(JVM):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.release = kwargs["release"]
        try:
            self.release = int(self.release)
        except ValueError:
            raise TypeError("The release of an OpenJDK has to be int-like")
        self.home: Path
        self.home = Path(kwargs["home"])
        if not self.home.exists():
            logging.warning("OpenJDK home {} doesn't exist".format(self.home))
        self.executable = self.home / "bin" / "java"
        if not self.executable.exists():
            logging.warning(
                "{} not found in OpenJDK home".format(self.executable))
        self.executable = self.executable.absolute()

    def get_executable(self) -> Path:
        return self.executable

    def __str__(self):
        return "{} OpenJDK {} {}".format(super().__str__(), self.release, self.home)


@register(Runtime)
class JikesRVM(JVM):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.home: Path
        self.home = Path(kwargs["home"])
        if not self.home.exists():
            logging.warning("JikesRVM home {} doesn't exist".format(self.home))
        self.executable = self.home / "rvm"
        if not self.home.exists():
            logging.warning(
                "{} not found in JikesRVM home".format(self.executable))
        self.executable = self.executable.absolute()

    def get_executable(self) -> Path:
        return self.executable

    def __str__(self):
        return "{} JikesRVM {}".format(super().__str__(), self.home)


class JavaScriptRuntime(Runtime):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.executable: Path
        self.executable = Path(kwargs["executable"])
        if not self.executable.exists():
            logging.warning(
                "JavaScriptRuntime executable {} doesn't exist".format(self.executable))
        self.executable = self.executable.absolute()

    def get_executable(self) -> Path:
        return self.executable


@register(Runtime)
class D8(JavaScriptRuntime):
    def __str__(self):
        return "{} d8 {}".format(super().__str__(), self.executable)

    def get_heapsize_modifier(self, size: int) -> Modifier:
        size_str = "{}".format(size)
        heapsize = JSArg(
            name="heap{}".format(size_str),
            val="--initial-heap-size={} --max-heap-size={}".format(
                size_str, size_str)
        )
        return heapsize

    def is_oom(self, output: bytes) -> bool:
        # The format is "Fatal javascript OOM in ..."
        # such as "Fatal javascript OOM in Reached heap limit"
        # or "Fatal javascript OOM in Ineffective mark-compacts near heap limit"
        return b"Fatal javascript OOM in" in output


@register(Runtime)
class SpiderMonkey(JavaScriptRuntime):
    def __str__(self):
        return "{} SpiderMonkey {}".format(super().__str__(), self.executable)

    def get_heapsize_modifier(self, size: int) -> Modifier:
        size_str = "{}".format(size)
        # FIXME doesn't seem to be working
        heapsize = JSArg(
            name="heap{}".format(size_str),
            val="--available-memory={}".format(size_str)
        )
        return heapsize

    def is_oom(self, output: bytes) -> bool:
        # FIXME not sure how to check for OOM for SpiderMonkey yet
        return False


@register(Runtime)
class JavaScriptCore(JavaScriptRuntime):
    def __str__(self):
        return "{} JavaScriptCore {}".format(super().__str__(), self.executable)

    def get_heapsize_modifier(self, size: int) -> Modifier:
        size_str = "{}".format(size)
        # FIXME doesn't seem to be working
        heapsize = JSArg(
            name="heap{}".format(size_str),
            val="--gcMaxHeapSize={}".format(size_str)
        )
        return heapsize

    def is_oom(self, output: bytes) -> bool:
        # FIXME not sure how to check for OOM for JavaScriptCore yet
        return False


@register(Runtime)
class OCaml(Runtime):
    SWITCH_PREFIX = "running-ng"
    RELOCATABLE_REPO = "git+https://github.com/dra27/opam-repository.git#relocatable"
    _opam_bin: Optional[str] = None

    # Pinned so switches provisioned at different times, or compared within
    # one run, get the same build tool. 3.22.1 cannot bootstrap on 5.6 trunk.
    # Before raising: build all macro benchmarks on the candidate and confirm
    # it bootstraps on trunk. Override per runtime with `dune_version:`.
    DUNE_VERSION = "3.24.0"

    # Switches provisioned by this process. Nothing records what built a
    # leftover switch, so by default it is wiped and rebuilt (~10-20 min);
    # RUNNING_REUSE_SWITCHES=1 reuses it instead.
    _switches_created_this_run: Set[str] = set()

    # Active switch before this run touched anything; re-selected on exit.
    _original_switch: Optional[str] = None
    _original_switch_captured: bool = False

    # Held for the lifetime of the run.
    _opam_lock_fh: Optional[Any] = None
    LOCK_BASENAME = "running-ng.lock"

    @staticmethod
    def _reuse_stale_switches() -> bool:
        return os.environ.get("RUNNING_REUSE_SWITCHES", "") not in ("", "0")

    @staticmethod
    def _safe_key(raw: str) -> str:
        sanitized = re.sub(r"[^a-zA-Z0-9._-]+", "_", raw).strip("._-")
        if sanitized:
            return sanitized
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        return "runtime-{}".format(digest)

    @staticmethod
    def _run_checked(cmd: List[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None):
        logging.info("Running command: %s", " ".join(str(c) for c in cmd))
        subprocess.run(cmd, check=True, cwd=str(cwd) if cwd else None, env=env)

    @staticmethod
    def _find_opam() -> str:
        """Newest available opam binary (~/.opam may have been initialised by a newer opam)."""
        if OCaml._opam_bin is not None:
            return OCaml._opam_bin
        import shutil
        candidates = []
        seen: set = set()
        search = list(os.environ.get("PATH", "").split(os.pathsep))
        for extra in ["/usr/local/bin", "/usr/bin", "/bin"]:
            if extra not in search:
                search.append(extra)
        for d in search:
            p = os.path.join(d, "opam")
            rp = os.path.realpath(p)
            if rp in seen or not os.path.isfile(p) or not os.access(p, os.X_OK):
                continue
            seen.add(rp)
            try:
                ver = subprocess.run(
                    [p, "--version"], capture_output=True, text=True
                ).stdout.strip()
                candidates.append((p, ver))
            except Exception:
                continue
        if not candidates:
            OCaml._opam_bin = shutil.which("opam") or "opam"
            return OCaml._opam_bin
        candidates.sort(key=lambda pv: [int(x) for x in pv[1].split(".")], reverse=True)
        OCaml._opam_bin = candidates[0][0]
        logging.info("Using opam: %s (version %s)", OCaml._opam_bin, candidates[0][1])
        return OCaml._opam_bin

    @staticmethod
    def _switch_exists(switch_name: str) -> bool:
        opam = OCaml._find_opam()
        result = subprocess.run(
            [opam, "switch", "list", "--short"],
            capture_output=True, text=True,
        )
        return switch_name in result.stdout.split()

    @staticmethod
    def _acquire_opam_lock() -> None:
        """Lock the opam root for the run, or fail loudly.

        Exclusive for a run that may delete switches, shared under
        RUNNING_REUSE_SWITCHES=1 (mutates nothing). flock is released by the
        kernel on death, so a killed run never wedges it.
        """
        if OCaml._opam_lock_fh is not None:
            return
        shared = OCaml._reuse_stale_switches()
        opam_root = OCaml._get_opam_root()
        # `opam var root` reports the configured path even if it does not exist yet.
        opam_root.mkdir(parents=True, exist_ok=True)
        lock_path = opam_root / OCaml.LOCK_BASENAME
        fh = lock_path.open("a+")
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        try:
            fcntl.flock(fh.fileno(), mode | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            holder = fh.read().strip() or "an unknown process"
            fh.close()
            raise OpamRootBusyError(
                "Another running-ng run is using the opam root {}.\n"
                "  holder: {}\n"
                "Refusing to start: this run would remove and rebuild opam "
                "switches that the other run is using, which would corrupt "
                "both.\n"
                "Wait for it to finish, or give this run its own opam root "
                "via OPAMROOT=/path/to/other/root.".format(
                    OCaml._get_opam_root(), holder)
            )
        OCaml._opam_lock_fh = fh
        fh.seek(0)
        fh.truncate()
        fh.write("pid={} mode={} cmd={}\n".format(
            os.getpid(), "shared" if shared else "exclusive",
            " ".join(sys.argv)))
        fh.flush()
        logging.debug("Acquired %s running-ng lock on %s",
                      "shared" if shared else "exclusive", lock_path)

    @staticmethod
    def release_opam_lock() -> None:
        """Release the opam-root lock if held. Idempotent."""
        fh = OCaml._opam_lock_fh
        if fh is None:
            return
        OCaml._opam_lock_fh = None
        try:
            fh.seek(0)
            fh.truncate()
            fh.flush()
        except OSError:
            pass
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    @staticmethod
    def _save_active_switch() -> None:
        """Record the active switch, lazily before the first mutation so runs
        without opam runtimes never shell out to opam."""
        if OCaml._original_switch_captured:
            return
        OCaml._original_switch_captured = True
        opam = OCaml._find_opam()
        result = subprocess.run(
            [opam, "switch", "show"], capture_output=True, text=True,
        )
        if result.returncode == 0:
            OCaml._original_switch = result.stdout.strip() or None
            logging.debug("Active opam switch before this run: %s",
                          OCaml._original_switch)

    @staticmethod
    def restore_active_switch() -> None:
        """Re-select the switch that was active before this run. Idempotent, never fatal."""
        original = OCaml._original_switch
        if original is None:
            return
        OCaml._original_switch = None
        if not OCaml._switch_exists(original):
            logging.warning(
                "Not restoring original opam switch '%s': it no longer exists.",
                original)
            return
        opam = OCaml._find_opam()
        result = subprocess.run(
            [opam, "switch", "set", original], capture_output=True, text=True,
        )
        if result.returncode == 0:
            logging.info("Restored original opam switch '%s'", original)
        else:
            logging.warning("Failed to restore original opam switch '%s': %s",
                            original, result.stderr.strip())

    @staticmethod
    def _remove_switch(switch_name: str) -> None:
        opam = OCaml._find_opam()
        OCaml._run_checked(
            [opam, "switch", "remove", switch_name, "--yes"])

    @staticmethod
    def _switch_prefix(switch_name: str) -> Optional[Path]:
        """Filesystem prefix of ``switch_name`` (asked of opam, so local switches resolve), or None."""
        opam = OCaml._find_opam()
        result = subprocess.run(
            [opam, "var", "prefix", "--switch={}".format(switch_name)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return None
        prefix = result.stdout.strip()
        return Path(prefix) if prefix else None

    @staticmethod
    def _assert_switch_usable(switch_name: str) -> None:
        """Refuse to reuse a switch with no working compiler (an interrupted
        provisioning leaves the name registered). Not rebuilt here: reuse mode
        holds only a shared lock and must not mutate.
        """
        prefix = OCaml._switch_prefix(switch_name)
        ocamlc = prefix / "bin" / "ocamlc" if prefix else None
        if ocamlc is not None and ocamlc.is_file() and os.access(ocamlc, os.X_OK):
            return
        raise RuntimeError(
            "opam switch '{}' is registered but has no usable compiler{}.\n"
            "RUNNING_REUSE_SWITCHES is set, so this run will not rebuild it — "
            "reuse mode takes only a shared opam lock and must not delete a "
            "switch another run may be using.\n"
            "This usually means an earlier provisioning was interrupted, "
            "leaving the switch half-built.\n"
            "Fix it either way:\n"
            "  - rerun without RUNNING_REUSE_SWITCHES, which rebuilds it from "
            "scratch; or\n"
            "  - opam switch remove {} --yes".format(
                switch_name,
                " at {}".format(ocamlc) if ocamlc else "",
                switch_name)
        )

    @staticmethod
    def _claim_switch(switch_name: str) -> bool:
        """True when the caller must create ``switch_name``; a switch from an
        earlier run is wiped first unless reuse mode is on."""
        if switch_name in OCaml._switches_created_this_run:
            logging.info(
                "Reusing opam switch '%s' (provisioned earlier in this run)",
                switch_name)
            return False
        from running.suite import is_dry_run
        if not is_dry_run():
            # Taken here, not at startup, so runs without opam runtimes never need opam.
            OCaml._acquire_opam_lock()
        if not OCaml._switch_exists(switch_name):
            return True
        if OCaml._reuse_stale_switches():
            OCaml._assert_switch_usable(switch_name)
            logging.warning(
                "Reusing pre-existing opam switch '%s' because "
                "RUNNING_REUSE_SWITCHES is set; its compiler and dune "
                "version are whatever an earlier run happened to install.",
                switch_name)
            OCaml._switches_created_this_run.add(switch_name)
            return False
        if is_dry_run():
            logging.warning(
                "Dry run: would remove and rebuild pre-existing opam switch "
                "'%s'; reusing it as-is instead.", switch_name)
            return False
        logging.info(
            "Removing pre-existing opam switch '%s' so this run provisions it "
            "from scratch (set RUNNING_REUSE_SWITCHES=1 to reuse instead)",
            switch_name)
        OCaml._save_active_switch()
        OCaml._remove_switch(switch_name)
        return True

    @staticmethod
    def _parse_opam_env(switch: str) -> Dict[str, str]:
        """Parse ``opam env`` output for *switch* into an environment dict."""
        opam = OCaml._find_opam()
        result = subprocess.run(
            [opam, "env", "--switch={}".format(switch), "--set-switch"],
            capture_output=True, text=True, check=True,
        )
        env = dict(os.environ)
        for line in result.stdout.splitlines():
            line = line.strip()
            if "=" not in line or "export" not in line:
                continue
            part = line.split(";")[0]  # KEY='VALUE'
            key, _, value = part.partition("=")
            env[key.strip()] = value.strip().strip("'\"")
        return env

    @staticmethod
    def _opam_compiler_source(kwargs: Dict[str, Any]) -> str:
        """Build the ``opam compiler create`` source spec from config kwargs.

        Maps config fields to the ``user/repo:ref`` format:
          version: "5.4.0"  ->  "ocaml/ocaml:5.4.0"
          commit: "abc123"  ->  "ocaml/ocaml:abc123"
          repo: "https://github.com/user/repo.git"  ->  "user/repo:ref"
        """
        version = kwargs.get("version")
        commit = kwargs.get("commit", kwargs.get("hash"))
        repo = kwargs.get("repo", "https://github.com/ocaml/ocaml.git")

        m = re.match(r"https?://github\.com/([^/]+)/([^/.]+)", repo)
        if not m:
            raise ValueError(
                "Cannot parse GitHub user/repo from repo URL: {}. "
                "opam-compiler requires a GitHub repository.".format(repo)
            )
        user, repo_name = m.group(1), m.group(2)
        ref = str(version) if version else str(commit)
        return "{}/{}:{}".format(user, repo_name, ref)

    @staticmethod
    def _ensure_switch(kwargs: Dict[str, Any], switch_name: str):
        """Create an opam switch via ``opam compiler create`` if needed, then install dune and ocamlfind."""
        if not OCaml._claim_switch(switch_name):
            return

        opam = OCaml._find_opam()
        source = OCaml._opam_compiler_source(kwargs)
        configure_args = kwargs.get("configure_args", [])
        OCaml._save_active_switch()

        cmd: List[str] = [
            opam, "compiler", "create", source,
            "--switch", switch_name,
        ]
        if configure_args:
            configure_cmd = "./configure " + " ".join(configure_args)
            cmd.extend(["--configure-command", configure_cmd])

        logging.info("Creating opam switch '%s' from source '%s'", switch_name, source)
        OCaml._run_checked(cmd)

        # The relocatable overlay repo (needed only for satellite switches) is
        # opt-in and scoped to this switch. Never add it with --set-default:
        # it shadows opam.ocaml.org for every later switch (ocaml/merlin#2108).
        if kwargs.get("relocatable"):
            logging.info("Adding relocatable overlay repo to switch '%s' "
                         "(relocatable: true)", switch_name)
            OCaml._run_checked([
                opam, "repo", "add", "relocatable", OCaml.RELOCATABLE_REPO,
                "--switch={}".format(switch_name),
            ])

        # A failure here is fatal: falling back to the tools switch's unpinned
        # dune would silently undo the pin. Use `dune_version:` instead.
        dune_pkg = "dune.{}".format(
            kwargs.get("dune_version", OCaml.DUNE_VERSION))
        try:
            OCaml._run_checked([
                opam, "install", dune_pkg, "ocamlfind",
                "--switch={}".format(switch_name), "--yes",
            ])
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                "Failed to install {}/ocamlfind in switch '{}'.\n"
                "Refusing to continue: benchmark binaries would be built with "
                "whatever dune happens to be on PATH (typically the tools "
                "switch's, which is installed unconstrained), so this run's "
                "results would not be reproducible and runtimes compared "
                "against each other could be built by different dune "
                "versions.\n"
                "If this compiler needs a different dune, set `dune_version:` "
                "on the runtime in your config.".format(dune_pkg, switch_name)
            ) from e
        OCaml._switches_created_this_run.add(switch_name)

    @staticmethod
    def _get_opam_root() -> Path:
        """Return the opam root directory (typically ~/.opam)."""
        opam = OCaml._find_opam()
        result = subprocess.run(
            [opam, "var", "root"],
            capture_output=True, text=True, check=True,
        )
        return Path(result.stdout.strip())

    @staticmethod
    def _ensure_satellite_switch(base_switch: str, satellite_name: str):
        """Create a per-benchmark satellite switch: an empty registered switch
        whose contents are replaced by a copy of the base switch. Requires the
        base runtime to be declared ``relocatable: true``, or the copied
        dune/ocamlfind point at the base switch's path.
        """
        if OCaml._switch_exists(satellite_name):
            logging.info("Reusing existing satellite switch '%s'", satellite_name)
            return

        import shutil

        opam = OCaml._find_opam()
        opam_root = OCaml._get_opam_root()
        base_dir = opam_root / base_switch
        satellite_dir = opam_root / satellite_name

        if not base_dir.is_dir():
            raise RuntimeError(
                "Base switch directory not found at {}".format(base_dir)
            )

        logging.info(
            "Creating satellite switch '%s' (copying from '%s')",
            satellite_name, base_switch,
        )
        OCaml._run_checked([
            opam, "switch", "create", satellite_name,
            "--empty", "--no-switch",
        ])

        shutil.rmtree(str(satellite_dir))

        def _ignore_heavy(directory: str, contents: List[str]) -> set:
            """Skip sources/ and build/ (~300MB)."""
            if os.path.basename(directory) == ".opam-switch":
                return {c for c in contents if c in ("sources", "build")}
            return set()

        shutil.copytree(
            str(base_dir), str(satellite_dir),
            ignore=_ignore_heavy, symlinks=True,
        )
        logging.info("Satellite switch '%s' ready", satellite_name)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.version: Optional[str] = kwargs.get("version")
        self.commit: Optional[str] = kwargs.get("commit", kwargs.get("hash"))
        self._satellite_switches: Dict[str, str] = {}  # benchmark_name -> switch_name

        executable = kwargs.get("executable")
        if executable:
            # pre-built executable, no switch management
            self.executable = Path(str(executable)).absolute()
            self._switch_name: Optional[str] = None
            if not self.executable.exists():
                logging.warning("OCaml executable {} doesn't exist".format(self.executable))
        else:
            if not self.version and not self.commit:
                raise KeyError(
                    "OCaml runtime requires either `executable` or one of "
                    "`version`/`commit`/`hash`."
                )
            if self.version and self.commit:
                raise ValueError("Use either `version` or `commit`/`hash`, not both.")

            self._switch_name = "{}-{}".format(self.SWITCH_PREFIX, self.name)
            # type(self): subclasses override how the switch is built
            type(self)._ensure_switch(kwargs, self._switch_name)

            opam = OCaml._find_opam()
            result = subprocess.run(
                [opam, "var", "bin", "--switch={}".format(self._switch_name)],
                capture_output=True, text=True, check=True,
            )
            bin_dir = Path(result.stdout.strip())
            self.executable = (bin_dir / "ocaml").absolute()
            if not self.executable.exists():
                raise RuntimeError(
                    "Switch '{}' created but ocaml binary not found at {}".format(
                        self._switch_name, self.executable
                    )
                )

    def get_executable(self) -> Path:
        return self.executable

    def get_switch_name(self) -> Optional[str]:
        """Opam switch name, or None in executable mode."""
        return self._switch_name

    def get_switch_env(self) -> Dict[str, str]:
        """Environment with the runtime's opam switch activated."""
        if self._switch_name is None:
            env = os.environ.copy()
            exe_dir = str(self.executable.parent)
            env["PATH"] = "{}:{}".format(exe_dir, env.get("PATH", ""))
            return env
        return OCaml._parse_opam_env(self._switch_name)

    def ensure_benchmark_switch(self, benchmark_name: str) -> str:
        """Create or reuse a per-benchmark satellite switch; returns its name."""
        if self._switch_name is None:
            raise RuntimeError(
                "Cannot create satellite switches in legacy executable mode"
            )
        cached = self._satellite_switches.get(benchmark_name)
        if cached and OCaml._switch_exists(cached):
            return cached

        satellite = "{}-{}".format(self._switch_name, self._safe_key(benchmark_name))
        OCaml._ensure_satellite_switch(self._switch_name, satellite)
        self._satellite_switches[benchmark_name] = satellite
        return satellite

    def get_benchmark_switch_env(self, benchmark_name: str) -> Dict[str, str]:
        """Environment with the benchmark's satellite switch activated."""
        satellite = self.ensure_benchmark_switch(benchmark_name)
        return OCaml._parse_opam_env(satellite)

    def get_benchmark_switch_name(self, benchmark_name: str) -> Optional[str]:
        """Satellite switch name for a benchmark, or None if not created."""
        return self._satellite_switches.get(benchmark_name)

    def get_cache_key(self) -> str:
        # Includes the runtime name: same version with different configure_args
        # must not share a cache entry.
        if self.commit:
            return "ocaml-commit-{}-{}".format(
                self._safe_key(self.name), self._safe_key(self.commit)
            )
        if self.version:
            return "ocaml-version-{}-{}".format(
                self._safe_key(self.name), self._safe_key(self.version)
            )
        return "ocaml-exec-{}-{}".format(
            self._safe_key(self.name),
            hashlib.sha256(str(self.executable).encode("utf-8")).hexdigest()[:12],
        )

    def get_heapsize_modifier(self, _size: int) -> Modifier:
        raise NotImplementedError(
            "Heap-size-based runs are not supported for OCaml runtime; use runbms with OCaml knobs via modifiers."
        )

    def is_oom(self, output: bytes) -> bool:
        lower = output.lower()
        for pattern in [b"out of memory", b"out_of_memory"]:
            if pattern in lower:
                return True
        return False

    def get_major_version(self) -> int:
        """OCaml major version as an integer."""
        if self.version:
            try:
                return int(str(self.version).split(".")[0])
            except (ValueError, IndexError):
                raise ValueError(
                    "Cannot parse major version from OCaml version string: {!r}".format(self.version)
                )
        result = subprocess.run(
            [str(self.executable), "--version"],
            capture_output=True, text=True, check=True
        )
        m = re.search(r"version\s+(\d+)\.", result.stdout)
        if not m:
            raise RuntimeError(
                "Cannot detect OCaml major version from executable output: {!r}".format(
                    result.stdout.strip()
                )
            )
        return int(m.group(1))


@register(Runtime)
class OCamlMMTk(OCaml):
    """OCaml built against MMTk (fplaunchpad/ocaml-mmtk).

    The heap is fixed, sized at run time by ``MMTK_HEAP_SIZE_MB`` (so minheap
    is well defined); the plan is chosen by ``MMTK_PLAN`` via an EnvVar
    modifier. Every MMTk process runs under ``setarch -R``, or the
    fixed-address metadata mmap fails with "failed to mmap meta memory: File exists".

    Config: ``type: OCamlMMTk`` with ``commit:`` (repo defaults to the fork)
    or ``executable:`` (pre-built tree).
    """

    DEFAULT_REPO = "https://github.com/fplaunchpad/ocaml-mmtk.git"

    def __init__(self, **kwargs):
        if not kwargs.get("executable") and "repo" not in kwargs:
            kwargs["repo"] = OCamlMMTk.DEFAULT_REPO
        super().__init__(**kwargs)

    def get_command_prefix(self) -> List[str]:
        # ASLR off for every MMTk process; the compiler build handles it via
        # opam's wrap-build-commands (see _ensure_switch).
        return ["setarch", os.uname().machine, "-R"]

    # Config modifiers apply only at run time, so builds need their own
    # heap size or large tools OOM while being compiled.
    BUILD_HEAP_SIZE_MB = "16384"

    def get_build_env_overrides(self) -> Dict[str, str]:
        overrides = {
            "MMTK_HEAP_SIZE_MB": os.environ.get(
                "MMTK_HEAP_SIZE_MB", OCamlMMTk.BUILD_HEAP_SIZE_MB
            )
        }
        # ocamlc -config lists a bare `-lmmtk_ocaml` without -L, so
        # dune-configurator probes (lwt, ctypes, owl) fail to link and
        # mis-detect features. LIBRARY_PATH lets ld find libmmtk_ocaml.a.
        stdlib = self.executable.parent.parent / "lib" / "ocaml"
        if (stdlib / "libmmtk_ocaml.a").exists():
            existing = os.environ.get("LIBRARY_PATH", "")
            overrides["LIBRARY_PATH"] = (
                "{}:{}".format(stdlib, existing) if existing else str(stdlib)
            )
        return overrides

    def get_heapsize_modifier(self, size: int) -> Modifier:
        # `size` is in MB, as MMTK_HEAP_SIZE_MB expects.
        return EnvVar(
            name="mmtk_heap_{}M".format(size),
            var="MMTK_HEAP_SIZE_MB",
            val=str(size),
        )

    # Replacing opam's sandbox wrapper with `setarch -R` does two things: drops
    # bubblewrap's --unshare-net so cargo can fetch crates during make, and
    # re-applies no-randomize on the build command itself (opam and bubblewrap
    # both reset the personality, so wrapping the outer opam is useless).
    _WRAP_KEYS = ("wrap-build-commands", "wrap-install-commands")

    @staticmethod
    def _set_opam_wrappers(opam: str, value: Optional[str],
                           saved: Optional[Dict[str, str]] = None) -> Optional[Dict[str, str]]:
        """Set the global build/install wrappers to *value* (an opam list literal),
        returning a snapshot; ``value=None`` with the snapshot restores them."""
        if value is not None:
            snap: Dict[str, str] = {}
            for k in OCamlMMTk._WRAP_KEYS:
                r = subprocess.run(
                    [opam, "option", "--global", k],
                    capture_output=True, text=True,
                )
                snap[k] = r.stdout.strip()
                subprocess.run(
                    [opam, "option", "--global", "{}={}".format(k, value)],
                    check=True, capture_output=True, text=True,
                )
            return snap
        for k in OCamlMMTk._WRAP_KEYS:
            orig = (saved or {}).get(k) or "[]"
            subprocess.run(
                [opam, "option", "--global", "{}={}".format(k, orig)],
                capture_output=True, text=True,
            )
        return None

    @staticmethod
    def _ensure_switch(kwargs: Dict[str, Any], switch_name: str):
        """Build the MMTk compiler switch with opam's wrappers temporarily set
        to ``setarch -R`` (see ``_WRAP_KEYS``). dune/ocamlfind are deliberately
        not installed here; builds use the tools switch's dune.
        """
        if not OCaml._claim_switch(switch_name):
            return

        opam = OCaml._find_opam()
        source = OCaml._opam_compiler_source(kwargs)
        configure_args = kwargs.get("configure_args", [])
        OCaml._save_active_switch()
        machine = os.uname().machine

        cmd: List[str] = [
            opam, "compiler", "create", source, "--switch", switch_name,
        ]
        if configure_args:
            cmd.extend(["--configure-command",
                        "./configure " + " ".join(configure_args)])

        env = dict(os.environ)
        cargo_bin = os.path.join(os.path.expanduser("~"), ".cargo", "bin")
        env["PATH"] = "{}:{}".format(cargo_bin, env.get("PATH", ""))
        env.setdefault("MMTK_HEAP_SIZE_MB", "8192")

        wrapper = '["setarch" "{}" "-R"]'.format(machine)
        saved = OCamlMMTk._set_opam_wrappers(opam, wrapper)
        try:
            logging.info(
                "Building MMTk compiler switch '%s' from '%s' "
                "(build wrapped in `setarch %s -R`, cargo on PATH)",
                switch_name, source, machine,
            )
            OCaml._run_checked(cmd, env=env)
            OCaml._switches_created_this_run.add(switch_name)
        finally:
            OCamlMMTk._set_opam_wrappers(opam, None, saved=saved)


@register(Runtime)
class OxCaml(OCaml):
    """OxCaml (Jane Street's OCaml fork): OCaml with a different default repo."""

    DEFAULT_REPO = "https://github.com/oxcaml/oxcaml.git"

    def __init__(self, **kwargs):
        if "repo" not in kwargs:
            kwargs["repo"] = OxCaml.DEFAULT_REPO
        super().__init__(**kwargs)
