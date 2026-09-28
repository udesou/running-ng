from running.modifier import JVMArg, Modifier, JSArg, EnvVar
from typing import Any, Dict, List, Optional, Set, Union
from pathlib import Path
import logging
from running.util import register
from running import opam_roots
import hashlib
import os
import re
import shutil
import subprocess
import sys
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
    RELOCATABLE_REPO = "git+https://github.com/dra27/opam-repository.git#relocatable"
    _opam_bin: Optional[str] = None

    # Pinned so switches provisioned at different times, or compared within
    # one run, get the same build tool. 3.22.1 cannot bootstrap on 5.6 trunk.
    # Before raising: build all macro benchmarks on the candidate and confirm
    # it bootstraps on trunk. Override per runtime with `dune_version:`.
    DUNE_VERSION = "3.24.0"

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
    def _opam_compiler_bin() -> str:
        """The opam-compiler plugin, run directly: runtime roots register no plugins."""
        override = os.environ.get("RUNNING_OPAM_COMPILER")
        if override:
            return override
        from running import switches
        r = subprocess.run(
            [OCaml._find_opam(), "var", "bin", "--switch", switches.TOOLS_SWITCH],
            capture_output=True, text=True, env=switches.tools_env())
        if r.returncode == 0:
            candidate = Path(r.stdout.strip()) / "opam-compiler"
            if candidate.is_file():
                return str(candidate)
        found = shutil.which("opam-compiler")
        if found:
            return found
        raise RuntimeError(
            "opam-compiler not found in the {} switch of running-ng's opam "
            "root, or on PATH. Run "
            "install_deps.sh, or set RUNNING_OPAM_COMPILER to the binary."
            .format(switches.TOOLS_SWITCH))

    @staticmethod
    def release_opam_lock() -> None:
        """Release the locks on every opam root this run used. Idempotent."""
        opam_roots.release_all()

    @staticmethod
    def _switch_exists(root: "opam_roots.Root", switch_name: str) -> bool:
        result = subprocess.run(
            [OCaml._find_opam(), "switch", "list", "--short"],
            capture_output=True, text=True, env=root.env(),
        )
        return switch_name in result.stdout.split()

    @staticmethod
    def _parse_opam_env(root: "opam_roots.Root", switch: str) -> Dict[str, str]:
        """The environment with *switch* of *root* activated."""
        result = subprocess.run(
            [OCaml._find_opam(), "env", "--switch={}".format(switch), "--set-switch"],
            capture_output=True, text=True, check=True, env=root.env(),
        )
        env = root.env()
        for line in result.stdout.splitlines():
            line = line.strip()
            if "=" not in line or "export" not in line:
                continue
            part = line.split(";")[0]  # KEY='VALUE'
            k, _, value = part.partition("=")
            env[k.strip()] = value.strip().strip("\'\"")
        return env

    @staticmethod
    def _opam_compiler_source(repo: str, ref: str) -> str:
        """``user/repo:ref``, the source spec ``opam compiler create`` takes."""
        m = re.match(r"https?://github\.com/([^/]+)/([^/.]+)", repo)
        if not m:
            raise ValueError(
                "Cannot parse GitHub user/repo from repo URL: {}. "
                "opam-compiler requires a GitHub repository.".format(repo)
            )
        return "{}/{}:{}".format(m.group(1), m.group(2), ref)

    @classmethod
    def _dune_version(cls, kwargs: Dict[str, Any]) -> Optional[str]:
        return kwargs.get("dune_version", OCaml.DUNE_VERSION)

    def _identity(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        return opam_roots.identity(
            type(self).__name__,
            kwargs.get("repo", "https://github.com/ocaml/ocaml.git"),
            str(self.version) if self.version else str(self.commit),
            configure_args=kwargs.get("configure_args"),
            dune_version=type(self)._dune_version(kwargs),
            relocatable=bool(kwargs.get("relocatable")),
            opam_repository=kwargs.get("opam_repository"),
        )

    @classmethod
    def _create_command(cls, ident: Dict[str, Any]) -> List[str]:
        cmd = [OCaml._opam_compiler_bin(), "create",
               OCaml._opam_compiler_source(ident["repo"], ident["sha"]),
               "--switch", opam_roots.SWITCH]
        if ident["configure_args"]:
            cmd.extend(["--configure-command",
                        "./configure " + " ".join(ident["configure_args"])])
        return cmd

    @classmethod
    def _build_root(cls, ident: Dict[str, Any], root: "opam_roots.Root") -> None:
        """Build the compiler at the identity's SHA, then dune and ocamlfind."""
        opam = OCaml._find_opam()
        OCaml._run_checked(cls._create_command(ident), env=root.env())

        # Overlay repo for satellite switches; scoped to the switch, never
        # --set-default (it would shadow the pinned repository).
        if ident["relocatable"]:
            OCaml._run_checked([
                opam, "repo", "add", "relocatable", OCaml.RELOCATABLE_REPO,
                "--switch={}".format(opam_roots.SWITCH),
            ], env=root.env())

        # Fatal: falling back to the tools switch's unpinned dune would
        # silently undo the pin. Use `dune_version:` instead.
        dune_pkg = "dune.{}".format(ident["dune_version"])
        try:
            OCaml._run_checked([
                opam, "install", dune_pkg, "ocamlfind",
                "--switch={}".format(opam_roots.SWITCH), "--yes",
            ], env=root.env())
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                "Failed to install {}/ocamlfind in opam root {}.\n"
                "Refusing to continue: benchmark binaries would be built with "
                "whatever dune happens to be on PATH, so runtimes compared "
                "against each other could be built by different dune versions.\n"
                "If this compiler needs a different dune, set `dune_version:` "
                "on the runtime in your config.".format(dune_pkg, root.path)
            ) from e

    def _ensure_satellite_switch(self, satellite_name: str) -> None:
        """A per-benchmark satellite switch in this runtime's root: an empty
        registered switch whose contents are a copy of the runtime switch.
        Needs ``relocatable: true``, or the copied dune/ocamlfind point at the
        runtime switch's path."""
        root = self._root
        if OCaml._switch_exists(root, satellite_name):
            logging.info("Reusing existing satellite switch '%s'", satellite_name)
            return
        logging.info("Creating satellite switch '%s' in %s", satellite_name, root.path)
        OCaml._run_checked([
            OCaml._find_opam(), "switch", "create", satellite_name,
            "--empty", "--no-switch",
        ], env=root.env())
        satellite_dir = root.path / satellite_name
        shutil.rmtree(str(satellite_dir))

        def _ignore_heavy(directory: str, contents: List[str]) -> set:
            """Skip sources/ and build/ (~300MB)."""
            if os.path.basename(directory) == ".opam-switch":
                return {c for c in contents if c in ("sources", "build")}
            return set()

        shutil.copytree(str(root.switch_prefix()), str(satellite_dir),
                        ignore=_ignore_heavy, symlinks=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.version: Optional[str] = kwargs.get("version")
        self.commit: Optional[str] = kwargs.get("commit", kwargs.get("hash"))
        self._satellite_switches: Dict[str, str] = {}  # benchmark_name -> switch_name
        self._compiler_identity: Optional[str] = None
        self._root: Optional[opam_roots.Root] = None

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
            if os.environ.get("RUNNING_REUSE_SWITCHES", "") not in ("", "0"):
                logging.warning("RUNNING_REUSE_SWITCHES is ignored: a runtime's "
                                "opam root is reused whenever its identity matches.")

            ident = self._identity(kwargs)
            self._root = opam_roots.ensure(
                ident, lambda root: type(self)._build_root(ident, root),
                opam=OCaml._find_opam())
            self._switch_name = opam_roots.SWITCH
            self._compiler_identity = ident["sha"]
            self.executable = (self._root.switch_prefix() / "bin" / "ocaml").absolute()
            if not self.executable.exists():
                raise RuntimeError(
                    "opam root {} is complete but has no ocaml binary at {}".format(
                        self._root.path, self.executable))

    def get_executable(self) -> Path:
        return self.executable

    def get_switch_name(self) -> Optional[str]:
        """Opam switch name, or None in executable mode."""
        return self._switch_name

    def get_switch_prefix(self) -> Optional[Path]:
        """Prefix of the runtime's switch, or None in executable mode."""
        return self._root.switch_prefix() if self._root else None

    def get_switch_env(self) -> Dict[str, str]:
        """Environment with the runtime's opam switch activated."""
        if self._root is None:
            env = os.environ.copy()
            exe_dir = str(self.executable.parent)
            env["PATH"] = "{}:{}".format(exe_dir, env.get("PATH", ""))
            return env
        return OCaml._parse_opam_env(self._root, opam_roots.SWITCH)

    def ensure_benchmark_switch(self, benchmark_name: str) -> str:
        """Create or reuse a per-benchmark satellite switch; returns its name."""
        if self._root is None:
            raise RuntimeError(
                "Cannot create satellite switches in legacy executable mode"
            )
        cached = self._satellite_switches.get(benchmark_name)
        if cached and OCaml._switch_exists(self._root, cached):
            return cached
        satellite = "{}-{}".format(opam_roots.SWITCH, self._safe_key(benchmark_name))
        self._ensure_satellite_switch(satellite)
        self._satellite_switches[benchmark_name] = satellite
        return satellite

    def get_benchmark_switch_env(self, benchmark_name: str) -> Dict[str, str]:
        """Environment with the benchmark's satellite switch activated."""
        satellite = self.ensure_benchmark_switch(benchmark_name)
        return OCaml._parse_opam_env(self._root, satellite)

    def get_compiler_identity(self) -> str:
        """The compiler's git SHA (resolved before its root was built); the
        executable's hash when there is no root."""
        if self._compiler_identity is None:
            h = hashlib.sha256(Path(self.executable).read_bytes()).hexdigest()
            self._compiler_identity = "executable:{}".format(h[:16])
        return self._compiler_identity

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
        # its root's wrap-build-commands (see _build_root).
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

    @classmethod
    def _dune_version(cls, kwargs: Dict[str, Any]) -> Optional[str]:
        # Builds use the tools switch's dune; nothing is installed here.
        return None

    @classmethod
    def _build_root(cls, ident: Dict[str, Any], root: "opam_roots.Root") -> None:
        """Build the MMTk compiler with this root's build/install wrappers set
        to ``setarch -R``: that drops bubblewrap's --unshare-net so cargo can
        fetch crates, and re-applies no-randomize on the build command itself
        (opam and bubblewrap both reset the personality). The root is MMTk's
        alone, so the wrappers stay set."""
        opam = OCaml._find_opam()
        machine = os.uname().machine
        for k in ("wrap-build-commands", "wrap-install-commands"):
            OCaml._run_checked([opam, "option", "--global",
                                '{}=["setarch" "{}" "-R"]'.format(k, machine)],
                               env=root.env())
        env = root.env()
        cargo_bin = os.path.join(os.path.expanduser("~"), ".cargo", "bin")
        env["PATH"] = "{}:{}".format(cargo_bin, env.get("PATH", ""))
        env.setdefault("MMTK_HEAP_SIZE_MB", "8192")
        OCaml._run_checked(cls._create_command(ident), env=env)


@register(Runtime)
class OxCaml(OCaml):
    """OxCaml (Jane Street's OCaml fork): OCaml with a different default repo."""

    DEFAULT_REPO = "https://github.com/oxcaml/oxcaml.git"
    # oxcaml/opam-repository's guard packages only admit its +ox dune builds.
    DUNE_VERSION = "3.22.2+ox"

    def __init__(self, **kwargs):
        if "repo" not in kwargs:
            kwargs["repo"] = OxCaml.DEFAULT_REPO
        kwargs.setdefault("dune_version", OxCaml.DUNE_VERSION)
        super().__init__(**kwargs)
        self._assert_is_oxcaml()

    def _assert_is_oxcaml(self):
        """Refuse a compiler that rejects mode syntax: a stock OCaml built
        from the OxCaml repo would otherwise pass for OxCaml."""
        ocamlopt = self.executable.parent / "ocamlopt"
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "modes.ml"
            src.write_text("let f (x @ local) = let _ = x in ()\n")
            r = subprocess.run([str(ocamlopt), "-c", str(src)],
                               capture_output=True, text=True, cwd=d)
        if r.returncode != 0:
            raise RuntimeError(
                "Runtime '{}' is not OxCaml: {} rejects mode syntax.\n{}".format(
                    self.name, ocamlopt, r.stderr.strip()))
