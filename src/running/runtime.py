from running.modifier import JVMArg, Modifier, JSArg
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
        OCaml._run_checked([
            opam, "install", "dune", "ocamlfind",
            "--switch={}".format(switch_name), "--yes",
        ])

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
            OCaml._ensure_switch(kwargs, self._switch_name)

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
        if self.commit:
            return "ocaml-commit-{}".format(self._safe_key(self.commit))
        if self.version:
            return "ocaml-version-{}".format(self._safe_key(self.version))
        return "ocaml-exec-{}".format(
            hashlib.sha256(str(self.executable).encode("utf-8")).hexdigest()[:12]
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
        # Route through OxCaml's build logic, not OCaml's.
        Runtime.__init__(self, **kwargs)
        self.executable = OxCaml._resolve_or_build_executable(kwargs)
        self.version: Optional[str] = kwargs.get("version")
        self.commit: Optional[str] = kwargs.get("commit", kwargs.get("hash"))
        if not self.executable.exists():
            logging.warning("OxCaml executable {} doesn't exist".format(self.executable))
        self.executable = self.executable.absolute()
