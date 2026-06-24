from running.modifier import JVMArg, Modifier, JSArg, EnvVar
from typing import Any, Dict, List, Optional, Union
from pathlib import Path
import logging
from running.util import register
import hashlib
import os
import re
import subprocess
import tempfile


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
        """Tokens prepended to every benchmark *build* and *run* command.

        Default is empty.  A runtime whose processes need a launcher wrapper
        (e.g. OCamlMMTk needs ``setarch -R`` to disable ASLR for MMTk's
        fixed-address metadata mmap) overrides this, so the requirement is
        carried by the runtime itself rather than a bespoke launch script.
        """
        return []

    def get_build_env_overrides(self) -> Dict[str, str]:
        """Env-var overrides for the benchmark *build* environment.

        Applied last (after the suite's build_env), so they take effect; a
        runtime that wants to preserve an existing/explicit value should fold
        it in itself (by reading the environment).  Default is empty.  See
        OCamlMMTk, which uses this to give the build a generous fixed MMTk heap
        and to put libmmtk_ocaml.a on LIBRARY_PATH.
        """
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
        """Find the best available opam binary.

        Prefers the newest version to avoid compatibility issues when
        ~/.opam was initialised by a newer opam.
        """
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
        """Create an opam switch via ``opam compiler create`` if needed.

        After building the compiler from source, the dra27 relocatable
        overlay repo is added to the switch so that ``dune`` and
        ``ocamlfind`` are installed as relocatable binaries.  This allows
        the switch to be copied for satellite switches without hardcoded
        paths breaking.
        """
        if OCaml._switch_exists(switch_name):
            logging.info("Reusing existing opam switch '%s'", switch_name)
            return

        opam = OCaml._find_opam()
        source = OCaml._opam_compiler_source(kwargs)
        configure_args = kwargs.get("configure_args", [])

        cmd: List[str] = [
            opam, "compiler", "create", source,
            "--switch", switch_name,
        ]
        if configure_args:
            configure_cmd = "./configure " + " ".join(configure_args)
            cmd.extend(["--configure-command", configure_cmd])

        logging.info("Creating opam switch '%s' from source '%s'", switch_name, source)
        OCaml._run_checked(cmd)

        # Add the relocatable overlay repo so dune/ocamlfind are installed
        # as relocatable binaries (no hardcoded paths in the binaries).
        logging.info("Adding relocatable overlay repo to switch '%s'", switch_name)
        OCaml._run_checked([
            opam, "repo", "add", "relocatable", OCaml.RELOCATABLE_REPO,
            "--switch={}".format(switch_name), "--set-default",
        ])

        # Install relocatable dune and ocamlfind.
        # This may fail on bleeding-edge trunk if dune is incompatible with
        # the compiler version.  In that case, the tools switch provides dune
        # via PATH (set up by run_ocaml_bench_gc_sweep.sh).
        try:
            OCaml._run_checked([
                opam, "install", "dune", "ocamlfind",
                "--switch={}".format(switch_name), "--yes",
            ])
        except subprocess.CalledProcessError:
            logging.warning(
                "Failed to install dune/ocamlfind in switch '%s'. "
                "Benchmark build scripts will use dune from the tools switch "
                "(ensure it is on PATH).",
                switch_name,
            )

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
        """Create a per-benchmark satellite switch by copying the base switch.

        1. Creates an empty opam switch (so opam registers it properly).
        2. Replaces its contents with a copy of the base switch's directory,
           skipping heavyweight build artifacts (sources/, build/).

        The base switch is set up with dra27's relocatable overlay, so the
        compiler tools (dune, ocamlfind) have no hardcoded paths and work
        correctly after being copied to a new location.
        ``opam env --switch=<satellite>`` regenerates the correct PATH.
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

        # Step 1: Let opam create an empty, properly registered switch.
        logging.info(
            "Creating satellite switch '%s' (copying from '%s')",
            satellite_name, base_switch,
        )
        OCaml._run_checked([
            opam, "switch", "create", satellite_name,
            "--empty", "--no-switch",
        ])

        # Step 2: Replace the empty switch contents with the base switch copy.
        shutil.rmtree(str(satellite_dir))

        def _ignore_heavy(directory: str, contents: List[str]) -> set:
            """Skip sources/ and build/ inside .opam-switch to save ~300MB."""
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
            # Legacy mode: pre-built executable, no switch management.
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
            # Dispatch via type(self) so subclasses (e.g. OCamlMMTk) can
            # override how the switch is built.
            type(self)._ensure_switch(kwargs, self._switch_name)

            # Resolve the executable from the switch's bin directory.
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
        """Return the opam switch name, or None for legacy executable mode."""
        return self._switch_name

    def get_switch_env(self) -> Dict[str, str]:
        """Return an environment dict with the runtime's opam switch activated."""
        if self._switch_name is None:
            env = os.environ.copy()
            exe_dir = str(self.executable.parent)
            env["PATH"] = "{}:{}".format(exe_dir, env.get("PATH", ""))
            return env
        return OCaml._parse_opam_env(self._switch_name)

    def ensure_benchmark_switch(self, benchmark_name: str) -> str:
        """Create (or reuse) a per-benchmark satellite switch.

        Returns the satellite switch name.  The satellite is a copy of the
        runtime's base switch (compiler binaries + stdlib + opam metadata)
        with its own independent opam package root for isolated installs.
        """
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
        """Return an environment dict with a per-benchmark satellite switch activated."""
        satellite = self.ensure_benchmark_switch(benchmark_name)
        return OCaml._parse_opam_env(satellite)

    def get_benchmark_switch_name(self, benchmark_name: str) -> Optional[str]:
        """Return the satellite switch name for a benchmark, or None if not created."""
        return self._satellite_switches.get(benchmark_name)

    def get_cache_key(self) -> str:
        # The cache key must uniquely identify the compiler being used — two
        # runtimes that build from the same version but with different
        # configure_args (e.g. --enable-frame-pointers vs --enable-flambda)
        # produce different binaries and must not share a binary cache entry.
        # Use the runtime's config-file name which is always unique.
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
        """Return the OCaml major version as an integer (e.g. 5 for '5.4.0')."""
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
    """OCaml built against MMTk (udesou/ocaml-mmtk).

    Identical to OCaml for build/switch purposes, but unlike stock OCaml the
    MMTk runtime has a *fixed* heap whose size is set at run time via the
    ``MMTK_HEAP_SIZE_MB`` environment variable.  That makes minheap binary
    search well-defined (stock OCaml grows its heap on demand and never OOMs
    on a fixed budget, which is why the base OCaml runtime is excluded from
    minheap measurement).

    The collector itself is chosen separately via ``MMTK_PLAN`` (Immix /
    StickyImmix for native code) — supply it as an EnvVar modifier in the
    config; minheap depends on the plan, so measure per plan.

    NOTE: every MMTk process must run with ASLR disabled (``setarch -R``);
    otherwise MMTk's fixed-address metadata mmap flakes with
    "failed to mmap meta memory: File exists".  Launch the whole pipeline
    (runbms / minheap) under setarch -R so all children inherit it.

    Config forms::

        # built + managed by running-ng (recommended, reproducible):
        mmtk:
          type: OCamlMMTk
          commit: "<sha or branch, e.g. 5.5+mmtk>"   # repo defaults to the fork

        # pre-built tree (no switch management):
        mmtk:
          type: OCamlMMTk
          executable: "/path/to/_install/bin/ocaml"
    """

    DEFAULT_REPO = "https://github.com/udesou/ocaml-mmtk.git"

    def __init__(self, **kwargs):
        # For commit/version-based builds, default the repo to the MMTk fork
        # (stock OCaml's default repo would be wrong).  Executable mode needs
        # no repo.
        if not kwargs.get("executable") and "repo" not in kwargs:
            kwargs["repo"] = OCamlMMTk.DEFAULT_REPO
        super().__init__(**kwargs)

    def get_command_prefix(self) -> List[str]:
        # Every MMTk process (benchmark build AND run) must run with ASLR
        # disabled, else MMTk's fixed-address metadata mmap flakes
        # ("failed to mmap meta memory: File exists").  Carrying this on the
        # runtime means the stock launch scripts work unchanged — no setarch
        # wrapper needed.  (The compiler build handles ASLR separately, via
        # opam's wrap-build-commands; see _ensure_switch.)
        return ["setarch", os.uname().machine, "-R"]

    # Fixed MMTk heap for benchmark builds.  MMTK_HEAP_SIZE_MB is unset during
    # builds (config modifiers apply only at run time), so MMTk would use a
    # small default heap and large tools (alt-ergo, frama-c) OOM while being
    # compiled/run during their build.  setdefault, so an explicit export or
    # build_env value still wins.
    BUILD_HEAP_SIZE_MB = "16384"

    def get_build_env_overrides(self) -> Dict[str, str]:
        # An explicit MMTK_HEAP_SIZE_MB export still wins.
        overrides = {
            "MMTK_HEAP_SIZE_MB": os.environ.get(
                "MMTK_HEAP_SIZE_MB", OCamlMMTk.BUILD_HEAP_SIZE_MB
            )
        }
        # MMTk puts a bare `-lmmtk_ocaml` (no -L) into ocamlc -config's
        # {bytecomp,native}_c_libraries.  Third-party dune-configurator feature
        # probes (lwt's pthread detect, ctypes' machdep, owl's cblas) link a
        # test program with those c_libraries but WITHOUT the compiler's stdlib
        # -L, so `ld` can't find -lmmtk_ocaml, the probe "fails", and the
        # library mis-detects the feature -> the real build then hits a
        # #error / missing symbol.  libmmtk_ocaml.a lives in the compiler's
        # stdlib dir, so putting that on LIBRARY_PATH lets ld resolve it.
        # (The proper fix belongs upstream in ocaml-mmtk: don't emit a bare
        # -lmmtk_ocaml in c_libraries — carry its -L or use an absolute path.)
        stdlib = self.executable.parent.parent / "lib" / "ocaml"
        if (stdlib / "libmmtk_ocaml.a").exists():
            existing = os.environ.get("LIBRARY_PATH", "")
            overrides["LIBRARY_PATH"] = (
                "{}:{}".format(stdlib, existing) if existing else str(stdlib)
            )
        return overrides

    def get_heapsize_modifier(self, size: int) -> Modifier:
        # `size` is in MB (minheap's binary search works in MB units, matching
        # the "{}M" labels it prints).  MMTK_HEAP_SIZE_MB takes MB directly.
        return EnvVar(
            name="mmtk_heap_{}M".format(size),
            var="MMTK_HEAP_SIZE_MB",
            val=str(size),
        )

    # opam build/install command wrappers.  Two MMTk-specific needs vs stock
    # OCaml drive these:
    #   1. cargo fetches crates DURING `make`, but opam's default wrapper
    #      (sandbox.sh) uses `--unshare-net` -> no network.  Replacing it with a
    #      plain `setarch` wrapper drops bubblewrap, so cargo can reach the net.
    #   2. MMTk's fixed-address metadata mmap flakes under ASLR.  Wrapping the
    #      build *command itself* with `setarch -R` is required because:
    #        - opam RESETS the no-randomize personality when it spawns builds
    #          (so wrapping the outer `opam` process is useless), and
    #        - bubblewrap also resets the personality to 0,
    #      so the no-randomize bit must be (re)applied on the actual build
    #      command, which is exactly what a wrap-build-commands wrapper does.
    _WRAP_KEYS = ("wrap-build-commands", "wrap-install-commands")

    @staticmethod
    def _set_opam_wrappers(opam: str, value: Optional[str],
                           saved: Optional[Dict[str, str]] = None) -> Optional[Dict[str, str]]:
        """Set the global build/install wrappers to *value* (an opam list
        literal), returning a snapshot of the previous values.  Pass
        ``value=None`` with the snapshot to restore the originals exactly
        (including the ``{os = ...}`` filter)."""
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
        """Build the MMTk compiler switch via ``opam compiler create``.

        Temporarily replaces opam's build/install wrappers with
        ``["setarch" "<arch>" "-R"]`` for the duration of the build (see
        ``_WRAP_KEYS`` for why), then restores them.

        dune/ocamlfind are intentionally NOT installed into the switch: the
        macro monorepo and micro builds use dune from the tools switch on PATH
        and the mmtk compiler (first on PATH) from this switch.
        """
        if OCaml._switch_exists(switch_name):
            logging.info("Reusing existing opam switch '%s'", switch_name)
            return

        opam = OCaml._find_opam()
        source = OCaml._opam_compiler_source(kwargs)
        configure_args = kwargs.get("configure_args", [])
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
        finally:
            OCamlMMTk._set_opam_wrappers(opam, None, saved=saved)


@register(Runtime)
class OxCaml(OCaml):
    """OxCaml (Jane Street's OCaml fork) runtime.

    Uses the OxCaml build system: autoconf + ./configure --enable-runtime5 +
    make install (Dune-based, no separate world.opt step).

    Automatically builds a stock OCaml bootstrap compiler (default 5.4.0)
    because OxCaml's configure requires OCaml 5.4.x on PATH.

    Config fields are the same as OCaml (repo, commit/version, executable,
    cache_dir, jobs) plus:
      - configure_args: extra args appended after --prefix and --enable-runtime5
      - bootstrap_version: stock OCaml version for bootstrapping (default "5.4.0")
    """

    DEFAULT_REPO = "https://github.com/oxcaml/oxcaml.git"
    DEFAULT_BOOTSTRAP_VERSION = "5.4.0"

    OPAM_SWITCH_NAME = "running-ng-oxcaml-build"

    @staticmethod
    def _ensure_bootstrap_compiler(kwargs: Dict[str, Any]) -> Path:
        """Build or locate a stock OCaml compiler for bootstrapping OxCaml.

        OxCaml's configure requires OCaml 5.4.x on PATH. Returns the bin/
        directory of the bootstrap compiler.
        """
        bootstrap_version = kwargs.get("bootstrap_version", OxCaml.DEFAULT_BOOTSTRAP_VERSION)
        bootstrap_kwargs = {
            "version": bootstrap_version,
            "cache_dir": kwargs.get("cache_dir",
                Path(tempfile.gettempdir()) / "running-ng-ocaml-toolchains"),
            "jobs": kwargs.get("jobs", os.cpu_count() or 1),
        }
        logging.info("Ensuring bootstrap compiler OCaml %s for OxCaml build", bootstrap_version)
        bootstrap_exe = OCaml._resolve_or_build_executable(bootstrap_kwargs)
        return bootstrap_exe.parent

    @staticmethod
    def _ensure_opam_switch(bootstrap_bin: Path) -> Dict[str, str]:
        """Create (or reuse) a dedicated opam switch for OxCaml builds.

        Uses the bootstrap OCaml compiler so that build tool versions are
        isolated from the user's active switch.  Returns an environ dict
        that activates the switch.
        """
        opam = OCaml._find_opam()
        switch = OxCaml.OPAM_SWITCH_NAME
        bootstrap_path_env = "{}:{}".format(bootstrap_bin, os.environ.get("PATH", ""))

        # Check whether the switch already exists.
        result = subprocess.run(
            [opam, "switch", "list", "--short"],
            capture_output=True, text=True,
        )
        existing_switches = result.stdout.split()

        if switch not in existing_switches:
            logging.info("Creating dedicated opam switch '%s' for OxCaml builds", switch)
            OCaml._run_checked([
                opam, "switch", "create", switch,
                "--packages=ocaml-system",
                "--no-install",
                "--yes",
            ], env=dict(os.environ, PATH=bootstrap_path_env))
        else:
            logging.info("Reusing existing opam switch '%s'", switch)

        # Obtain the switch environment.
        # opam env outputs lines like: KEY='VALUE'; export KEY;
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

        # Ensure bootstrap compiler is first on PATH so opam uses it.
        env["PATH"] = "{}:{}".format(bootstrap_bin, env.get("PATH", ""))

        return env

    @staticmethod
    def _install_opam_deps(source_dir: Path, env: Dict[str, str]):
        """Install OxCaml build deps from the repo's opam file into the switch.

        Reads the oxcaml-dev.opam file to find pinned dependency versions
        (e.g. menhir {= "20231231"}) and installs them at the exact required
        version.  Also ensures dune is installed.
        """
        opam = OCaml._find_opam()
        switch = OxCaml.OPAM_SWITCH_NAME
        opam_file = source_dir / "oxcaml-dev.opam"
        deps_to_install: List[str] = []

        if opam_file.exists():
            content = opam_file.read_text()
            # Match lines like: "menhir" {= "20231231"}
            for m in re.finditer(r'"(\w[\w-]*?)"\s*\{=\s*"([^"]+)"\}', content):
                pkg, ver = m.group(1), m.group(2)
                deps_to_install.append("{}.{}".format(pkg, ver))
                logging.info("OxCaml opam file requires %s = %s", pkg, ver)

        # Always ensure dune is present.
        if not any(d.startswith("dune.") or d == "dune" for d in deps_to_install):
            deps_to_install.append("dune")

        for dep in deps_to_install:
            # dep is either "pkg.ver" or "pkg"
            pkg_name = dep.split(".")[0]
            check = subprocess.run(
                [opam, "list", "--installed", "--short", pkg_name,
                 "--switch={}".format(switch)],
                capture_output=True, text=True,
            )
            installed = check.stdout.strip().split()
            if pkg_name in installed:
                # Check if the installed version matches.
                if "." in dep:
                    ver_check = subprocess.run(
                        [opam, "list", "--installed", dep.split(".")[0],
                         "--switch={}".format(switch), "--columns=version", "--short"],
                        capture_output=True, text=True,
                    )
                    installed_ver = ver_check.stdout.strip()
                    required_ver = dep.split(".", 1)[1]
                    if installed_ver == required_ver:
                        logging.info("%s already installed at correct version %s", pkg_name, installed_ver)
                        continue
                    logging.info("Reinstalling %s: have %s, need %s", pkg_name, installed_ver, required_ver)
                else:
                    logging.info("%s already installed", pkg_name)
                    continue

            logging.info("Installing %s in opam switch '%s'", dep, switch)
            OCaml._run_checked([
                opam, "install", dep, "--switch={}".format(switch), "--yes",
            ], env=env)

    @staticmethod
    def _resolve_or_build_executable(kwargs: Dict[str, Any]) -> Path:
        # Default repo to OxCaml if not specified.
        if "repo" not in kwargs:
            kwargs = dict(kwargs, repo=OxCaml.DEFAULT_REPO)

        built_executable, source_dir, install_dir, jobs = OCaml._clone_and_checkout(kwargs)
        if source_dir is None:
            return built_executable

        configure_args = kwargs.get("configure_args", [])
        if not isinstance(configure_args, list):
            raise TypeError("OxCaml runtime configure_args must be a list")

        # Ensure a suitable bootstrap compiler (OCaml 5.4.x) is on PATH and
        # build inside a dedicated opam switch so that menhir/dune versions
        # are compatible with OxCaml (not leaked from the user's switch).
        bootstrap_bin = OxCaml._ensure_bootstrap_compiler(kwargs)
        build_env = OxCaml._ensure_opam_switch(bootstrap_bin)
        OxCaml._install_opam_deps(source_dir, build_env)
        logging.info("OxCaml bootstrap compiler: %s", bootstrap_bin)

        # OxCaml needs autoconf to generate ./configure from configure.ac.
        OCaml._run_checked(["autoconf"], cwd=source_dir, env=build_env)
        OCaml._run_checked(
            ["./configure", "--prefix={}".format(install_dir), "--enable-runtime5"] + configure_args,
            cwd=source_dir, env=build_env
        )
        # OxCaml's `make install` builds the compiler + stdlib and installs.
        OCaml._run_checked(["make", "-j", str(jobs), "install"], cwd=source_dir, env=build_env)

        if not built_executable.exists():
            raise RuntimeError(
                "OxCaml build finished but executable not found at {}".format(built_executable)
            )
        return built_executable

    def __init__(self, **kwargs):
        # Default to OxCaml repo if not specified, then delegate to OCaml's
        # switch-based build via opam compiler create.
        if "repo" not in kwargs:
            kwargs["repo"] = OxCaml.DEFAULT_REPO
        super().__init__(**kwargs)
