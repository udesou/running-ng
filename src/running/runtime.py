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
    def _run_checked(cmd: List[str], cwd: Optional[Path] = None):
        logging.info("Running command: %s", " ".join(cmd))
        subprocess.run(cmd, check=True, cwd=str(cwd) if cwd else None)

    @staticmethod
    def _resolve_or_build_executable(kwargs: Dict[str, Any]) -> Path:
        executable = kwargs.get("executable")
        version = kwargs.get("version")
        commit = kwargs.get("commit", kwargs.get("hash"))
        repo = kwargs.get("repo", "https://github.com/ocaml/ocaml.git")
        configure_args = kwargs.get("configure_args", [])
        make_targets = kwargs.get("make_targets", ["world.opt"])
        jobs = kwargs.get("jobs", os.cpu_count() or 1)
        if not isinstance(configure_args, list):
            raise TypeError("OCaml runtime configure_args must be a list")
        if not isinstance(make_targets, list) or len(make_targets) == 0:
            raise TypeError("OCaml runtime make_targets must be a non-empty list")
        jobs = int(jobs)

        if executable:
            return Path(str(executable)).absolute()

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
            return built_executable

        toolchain_dir.mkdir(parents=True, exist_ok=True)
        if not source_dir.exists():
            OCaml._run_checked(["git", "clone", "--recursive", repo, str(source_dir)])
        else:
            OCaml._run_checked(["git", "fetch", "--all", "--tags"], cwd=source_dir)

        checkout_ref = str(commit) if commit else str(version)
        OCaml._run_checked(["git", "checkout", checkout_ref], cwd=source_dir)
        OCaml._run_checked(["git", "submodule", "update", "--init", "--recursive"], cwd=source_dir)
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
