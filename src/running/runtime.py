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

    # Pinned so switch provisioning is reproducible over time.  Installing an
    # unconstrained `dune` made the toolchain a function of *when* the switch
    # was created: switches provisioned before 2026-07 got dune 3.22.x, while
    # any created later resolved dune >= 3.24, which deleted the `coq`
    # language extension that macro-benches' vendored rocq declared
    # (`(using coq 0.8)`) — a parse error, so every benchmark build in the new
    # switch failed, not just the Coq one.
    #
    # macro-benches setup patches 19/20 strip those dead `coq` declarations, so
    # the >= 3.24 ceiling is gone and the workspace parses under both.
    #
    # 3.24.0 rather than 3.22.1 because 3.22.1 **cannot bootstrap against 5.6
    # trunk**.  The install failed, and the old code merely warned and let the
    # build fall through to whatever dune the tools switch had — which defeated
    # the pin in the one place it matters most.  A single 5.5.0-vs-trunk
    # comparison built its two sides with *different* dune versions (3.22.1 from
    # the 5.5.0 switch, 3.24.0 from the tools switch), making the build tool a
    # confound in the measurement.  Worse, the tools switch installs `dune`
    # unconstrained, so the fallback silently swapped a pinned build tool for an
    # unpinned one, and a machine with no tools switch had no dune at all.
    #
    # Validated on this pin: 3.24.0 installs cleanly into both a 5.5.0 and a
    # 5.6.0+dev trunk switch, and builds all 31 macro benchmarks on both.
    #
    # Before raising it again: (a) build all benchmarks on the candidate dune,
    # not just a few; (b) confirm it bootstraps on trunk, not only on the current
    # release; and (c) make sure every checkout has rerun `make setup`, since an
    # already-populated duniverse/ keeps the old dune-project until then.
    # Override per-runtime with `dune_version:` in the runtime's YAML block.
    DUNE_VERSION = "3.24.0"

    # Switches provisioned during *this* process.  A switch left over from an
    # earlier run may have been built with a different compiler source or a
    # different (then-current) dune, and nothing records which — so by default
    # a stale switch is wiped and rebuilt rather than silently reused.  Set
    # RUNNING_REUSE_SWITCHES=1 to keep the old reuse-if-present behaviour,
    # which matters for long sweeps: recreating a switch recompiles the
    # compiler from source (~10-20 min each).
    _switches_created_this_run: Set[str] = set()

    # The switch that was active before this run provisioned anything, so it
    # can be re-selected afterwards (see restore_active_switch).
    _original_switch: Optional[str] = None
    _original_switch_captured: bool = False

    # Open handle on the opam root's running-ng lock, held for the lifetime of
    # the run (see _acquire_opam_lock).
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
    def _acquire_opam_lock() -> None:
        """Serialise runs that share an opam root, or fail loudly.

        Two concurrent runs sharing an opam root can corrupt each other: the
        second one's delete-and-recreate would wipe a switch the first is
        actively building or benchmarking against.  There is no way to make
        that safe after the fact, so we refuse to start instead.

        The lock is taken **exclusively** by a run that may delete switches
        (the default) and **shared** by one running with
        RUNNING_REUSE_SWITCHES=1, which mutates nothing.  So any number of
        reuse-mode runs may overlap, but a destructive run will neither start
        alongside them nor let one start alongside it.

        Held for the lifetime of the process and released by
        :meth:`release_opam_lock`.  ``flock`` is released by the kernel when
        the process dies, so a crashed or killed run never wedges the lock.
        """
        if OCaml._opam_lock_fh is not None:
            return
        shared = OCaml._reuse_stale_switches()
        opam_root = OCaml._get_opam_root()
        # `opam var root` reports the configured path whether or not it exists
        # yet, so on a machine with no opam root the open() below would fail.
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
        """Release the opam-root lock, if this run holds one.  Idempotent."""
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
        """Record the switch that was active before we touched anything.

        Called lazily, immediately before the first mutation, so that runs
        which never provision a switch (or non-OCaml runtimes) don't shell out
        to opam at all.  Restored by :meth:`restore_active_switch`.
        """
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
        """Re-select whatever switch was active before this run.

        ``opam switch remove`` on the active switch leaves the root with no
        switch selected, and ``opam compiler create`` selects the switch it
        builds — either way the user's shell would be left pointing somewhere
        they didn't ask for.  Idempotent and never fatal: a run that already
        succeeded must not fail in cleanup.
        """
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
        """Filesystem prefix of ``switch_name``, or None if opam won't say.

        Asked of opam rather than assumed to be ``$OPAMROOT/<name>``, so local
        (path-based) switches resolve correctly too.
        """
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
        """Refuse to reuse a switch that isn't a working compiler.

        A switch is *registered* by opam before its compiler finishes building,
        so an interrupted provisioning (Ctrl-C, a timeout, a killed CI job)
        leaves the name present with no compiler behind it.  A normal run heals
        that on its own, because it removes and rebuilds a stale switch anyway.
        Reuse mode does not: it would hand the empty shell to the build scripts,
        which fail much later and far from the cause — a missing ``ocamlc``
        surfaces as a benchmark build error, not as "your switch is broken".

        Rebuilding it here is deliberately *not* the answer.  Reuse mode holds
        only a **shared** opam lock, precisely because it is supposed to mutate
        nothing; deleting a switch under that lock could pull it out from under
        a concurrent reuse-mode run.  So refuse, and say exactly what to do.
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
        """Decide whether ``switch_name`` still needs to be created.

        A switch provisioned earlier in *this* process is reused.  One left
        over from an earlier run is wiped first: nothing records which
        compiler source or dune version built it, so reusing it silently
        makes the toolchain a function of run history.  Returns True when the
        caller must go on to create the switch.
        """
        if switch_name in OCaml._switches_created_this_run:
            logging.info(
                "Reusing opam switch '%s' (provisioned earlier in this run)",
                switch_name)
            return False
        from running.suite import is_dry_run
        if not is_dry_run():
            # Before touching anything: claim the opam root, or refuse to run.
            # Taken here rather than at startup so that runs with no opam
            # runtimes (JVM, JS) never need opam to exist at all.
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
        """Create an opam switch via ``opam compiler create`` if needed.

        A switch left over from an earlier run is removed and rebuilt first —
        see :meth:`_claim_switch`.

        After building the compiler from source, the dra27 relocatable
        overlay repo is added to the switch so that ``dune`` and
        ``ocamlfind`` are installed as relocatable binaries.  This allows
        the switch to be copied for satellite switches without hardcoded
        paths breaking.
        """
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

        # The dra27 relocatable overlay repo is opt-in per runtime
        # (`relocatable: true`), and scoped to this switch alone.
        #
        # It used to be added to every switch with `--set-default`, which wrote
        # it into the opam *root's* default repository set at priority 1.  Two
        # consequences, both bad: running one benchmark permanently
        # reconfigured the user's opam installation, and from then on the fork
        # shadowed opam.ocaml.org for every switch they created afterwards —
        # including switches that have nothing to do with benchmarking.  Where
        # a version number exists in both repos (e.g. 5.5.0) the fork won,
        # silently substituting a development snapshot for the official
        # release.  That broke a third party's unrelated merlin install
        # (ocaml/merlin#2108) and this repo's own tools switch, which acquired
        # `ocaml-base-compiler.5.5.0` = a 2025-04-28 snapshot of 5.5 lacking
        # `Ptyp_functor`, so ppxlib's `ast_505.ml` no longer type-checked
        # against it.
        #
        # Relocatable support is upstreamed, so nothing here needs the overlay:
        # its only purpose is relocatable dune/ocamlfind for *satellite*
        # switches (`_ensure_satellite_switch` copies a switch directory, which
        # requires binaries with no hardcoded paths), and no shipped config
        # uses those.  Enable it explicitly if you revive that path.
        if kwargs.get("relocatable"):
            logging.info("Adding relocatable overlay repo to switch '%s' "
                         "(relocatable: true)", switch_name)
            OCaml._run_checked([
                opam, "repo", "add", "relocatable", OCaml.RELOCATABLE_REPO,
                "--switch={}".format(switch_name),
            ])

        # Install dune and ocamlfind.  dune is version-pinned (see DUNE_VERSION)
        # so that two switches provisioned months apart — and, just as
        # importantly, two switches compared within one run — get the same build
        # tool.
        #
        # A failure here is fatal.  It used to warn and fall through to whatever
        # dune the tools switch happened to have on PATH, which quietly undid the
        # pin: the tools switch installs `dune` unconstrained, so the fallback
        # substituted an unpinned build tool for a pinned one, and it kicked in
        # exactly where reproducibility matters most (a trunk switch, whose dune
        # is the one most likely to fail to bootstrap).  A 5.5.0-vs-trunk run
        # built its two sides with different dune versions and said nothing but a
        # WARNING.  A machine with no tools switch got no dune at all.
        #
        # If a compiler genuinely needs a different dune, say so explicitly with
        # `dune_version:` on that runtime rather than relying on a fallback.
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
        """Create a per-benchmark satellite switch by copying the base switch.

        1. Creates an empty opam switch (so opam registers it properly).
        2. Replaces its contents with a copy of the base switch's directory,
           skipping heavyweight build artifacts (sources/, build/).

        This requires the base switch's runtime to have been declared with
        ``relocatable: true``, so that its dune/ocamlfind carry no hardcoded
        paths and keep working after the directory is copied.  Without it the
        copy inherits binaries pointing at the base switch's path.  (The
        overlay used to be added to every switch unconditionally; see
        :meth:`_ensure_switch` for why that had to stop.)
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
