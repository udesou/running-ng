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
    @staticmethod
    def _safe_key(raw: str) -> str:
        sanitized = re.sub(r"[^a-zA-Z0-9._-]+", "_", raw).strip("._-")
        if sanitized:
            return sanitized
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        return "runtime-{}".format(digest)

    @staticmethod
    def _run_checked(cmd: List[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None):
        logging.info("Running command: %s", " ".join(cmd))
        subprocess.run(cmd, check=True, cwd=str(cwd) if cwd else None, env=env)

    @staticmethod
    def _clone_and_checkout(kwargs: Dict[str, Any]):
        """Clone/fetch the repo and check out the right ref.

        Returns (source_dir, install_dir, built_executable, jobs) or
        raises if the executable is already cached (via the returned Path).
        """
        executable = kwargs.get("executable")
        version = kwargs.get("version")
        commit = kwargs.get("commit", kwargs.get("hash"))
        repo = kwargs.get("repo", "https://github.com/ocaml/ocaml.git")
        jobs = int(kwargs.get("jobs", os.cpu_count() or 1))

        if executable:
            return Path(str(executable)).absolute(), None, None, None

        if not version and not commit:
            raise KeyError(
                "OCaml runtime requires either `executable` or one of `version`/`commit`/`hash`."
            )
        if version and commit:
            raise ValueError("Use either `version` or `commit`/`hash`, not both.")

        ref = str(commit) if commit else str(version)
        key = "commit-{}".format(ref) if commit else "version-{}".format(ref)
        root = Path(kwargs.get(
            "cache_dir",
            Path(tempfile.gettempdir()) / "running-ng-ocaml-toolchains"
        )).expanduser().absolute()
        toolchain_dir = root / OCaml._safe_key(key)
        source_dir = toolchain_dir / "src"
        install_dir = toolchain_dir / "install"
        built_executable = install_dir / "bin" / "ocaml"

        if built_executable.exists():
            logging.info("Using cached OCaml runtime at %s", built_executable)
            return built_executable, None, None, None

        toolchain_dir.mkdir(parents=True, exist_ok=True)
        if not source_dir.exists():
            OCaml._run_checked(["git", "clone", "--recursive", repo, str(source_dir)])
        else:
            OCaml._run_checked(["git", "fetch", "--all", "--tags"], cwd=source_dir)

        checkout_ref = str(commit) if commit else str(version)
        OCaml._run_checked(["git", "checkout", checkout_ref], cwd=source_dir)
        OCaml._run_checked(["git", "submodule", "update", "--init", "--recursive"], cwd=source_dir)

        return built_executable, source_dir, install_dir, jobs

    @staticmethod
    def _resolve_or_build_executable(kwargs: Dict[str, Any]) -> Path:
        built_executable, source_dir, install_dir, jobs = OCaml._clone_and_checkout(kwargs)
        if source_dir is None:
            # Already cached or using a pre-built executable.
            return built_executable

        configure_args = kwargs.get("configure_args", [])
        make_targets = kwargs.get("make_targets", ["world.opt"])
        if not isinstance(configure_args, list):
            raise TypeError("OCaml runtime configure_args must be a list")
        if not isinstance(make_targets, list) or len(make_targets) == 0:
            raise TypeError("OCaml runtime make_targets must be a non-empty list")

        OCaml._run_checked(["./configure", "--prefix={}".format(install_dir)] + configure_args, cwd=source_dir)
        OCaml._run_checked(["make", "-j", str(jobs)] + make_targets, cwd=source_dir)
        OCaml._run_checked(["make", "install"], cwd=source_dir)

        if not built_executable.exists():
            raise RuntimeError(
                "OCaml build finished but executable not found at {}".format(built_executable)
            )
        return built_executable

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.executable = OCaml._resolve_or_build_executable(kwargs)
        self.version: Optional[str] = kwargs.get("version")
        self.commit: Optional[str] = kwargs.get("commit", kwargs.get("hash"))
        if not self.executable.exists():
            logging.warning("OCaml executable {} doesn't exist".format(self.executable))
        self.executable = self.executable.absolute()

    def get_executable(self) -> Path:
        return self.executable

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
        # Fall back to querying the executable
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

        # Ensure a suitable bootstrap compiler (OCaml 5.4.x) is on PATH.
        bootstrap_bin = OxCaml._ensure_bootstrap_compiler(kwargs)
        build_env = dict(os.environ)
        build_env["PATH"] = "{}:{}".format(bootstrap_bin, build_env.get("PATH", ""))
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
