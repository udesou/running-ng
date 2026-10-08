"""olly (runtime_events_tools), built with each runtime's own compiler.

olly's source and its vendored dependencies are prepared once per olly commit
under ``$RUNNING_OPAM_ROOTS/olly``; each runtime then builds it into
``<its root>/olly/<key>``, so nothing is installed in the runtime's switch.

Which olly: ``OLLY_COMMIT`` (default :data:`COMMIT`), or the working tree at
``OLLY_DIR``; ``OLLY_BIN`` skips all of this and uses one olly for every
runtime. The dependency lock here is for :data:`COMMIT`; another commit with
different dependencies is re-locked into the cache (needs opam-monorepo).
"""
import argparse
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from running import opam_roots

REPO = "https://github.com/tarides/runtime_events_tools.git"
COMMIT = "e31d9081e3de71a4c3e72c9a0b7a3ca336b62f92"

COMMIT_ENV_VAR = "OLLY_COMMIT"
DIR_ENV_VAR = "OLLY_DIR"
BIN_ENV_VAR = "OLLY_BIN"

HERE = Path(__file__).resolve().parent
LOCK = HERE / "olly-deps.opam.locked"
#: olly's own depends for COMMIT, as the lock was made from them.
DEPENDS = HERE / "olly-depends"
PATCHES = HERE / "patches"
OVERLAYS = HERE / "overlays"

#: Opam repositories the lock is resolved against.
OPAM_OVERLAYS = ("git+https://github.com/dune-universe/opam-overlays.git"
                 "#632df22c65362cb34fd792f701d7d2a6518cdb60")
#: System libraries olly's deps build against; not vendored.
OPAM_PROVIDED = ["conf-pkg-config", "conf-cmake", "conf-zlib", "conf-libffi"]
#: OxCaml roots use dune 3.22.2+ox; dune-configurator must not need newer.
LOCK_CONSTRAINTS = ['"dune" {< "3.23"}', '"dune-configurator" {< "3.23"}']
OPAM_MONOREPO = "opam-monorepo.0.4.3"

#: Dependencies with no dune port for the version olly needs; built from
#: their release tarball with the dune files in overlays/<name>.
OVERLAY_SOURCES = {
    "jsont": ("https://erratique.ch/software/jsont/releases/jsont-0.4.0.tbz",
              "992b7b8470a40c5c6bb1513c86b8337abf21b09388bddaf89e150a344e3e1b56"),
    "bytesrw": ("https://erratique.ch/software/bytesrw/releases/bytesrw-0.4.0.tbz",
                "60962ed5ab696577bdb314368889314387f6554a86690154273c233159c82af5"),
}

MARKER = ".running-ng-olly.json"
#: Bumped when prepare() changes what it writes, so old trees are not reused.
PREPARE_VERSION = 2
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def cache_dir() -> Path:
    return opam_roots.roots_dir() / opam_roots.OLLY_CACHE


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(*args: str, cwd: Optional[Path] = None) -> str:
    return subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


# --- olly's depends ----------------------------------------------------------

def parse_depends(opam_text: str) -> List[str]:
    """olly's build dependencies, one normalised ``"name" {constraint}`` each."""
    m = re.search(r"^depends:\s*\[(.*?)^\]", opam_text, re.S | re.M)
    if not m:
        raise RuntimeError("no depends: field in runtime_events_tools.opam")
    out = []
    for name, constraint in re.findall(r'"([^"]+)"\s*(\{[^}]*\})?', m.group(1)):
        if re.search(r"with-test|with-doc|with-dev-setup", constraint or ""):
            continue
        out.append(" ".join(('"{}" {}'.format(name, constraint)).split()).strip())
    return out


def _name(dep: str) -> str:
    return dep.split('"')[1]


def lock_input(depends: List[str]) -> str:
    """The opam file opam-monorepo locks for these depends."""
    deps = [d for d in depends
            if _name(d) not in OVERLAY_SOURCES and _name(d) not in ("ocaml", "dune")]
    return "\n".join([
        'opam-version: "2.0"',
        'synopsis: "olly\'s dependencies, vendored by running-ng"',
        "depends: [",
    ] + ["  " + d for d in deps + LOCK_CONSTRAINTS] + [
        "]",
        "x-opam-monorepo-opam-provided: [ {} ]".format(
            " ".join('"{}"'.format(p) for p in OPAM_PROVIDED)),
        "x-opam-monorepo-opam-repositories: [",
        '  "{}"'.format(OPAM_OVERLAYS),
        '  "git+{}.git#{}"'.format(opam_roots.OPAM_REPOSITORY_URL,
                                   opam_roots.OPAM_REPOSITORY_COMMIT),
        "]",
    ]) + "\n"


def _committed_depends() -> List[str]:
    return [line for line in DEPENDS.read_text().splitlines() if line.strip()]


def _relock(depends: List[str], out: Path, check_overlays: bool = True) -> None:
    """Lock ``depends`` with opam-monorepo into ``out``."""
    from running import switches
    pinned = {_name(d): d for d in _committed_depends()} if check_overlays else {}
    for d in depends:
        if check_overlays and _name(d) in OVERLAY_SOURCES and pinned.get(_name(d)) != d:
            raise RuntimeError(
                "olly now needs {}, but running-ng builds {} from an overlay "
                "(src/running/olly/overlays); update the overlay".format(d, _name(d)))
    exe = switches.ensure_tool("opam-monorepo", OPAM_MONOREPO)
    with tempfile.TemporaryDirectory(prefix="olly-lock-") as tmp:
        (Path(tmp) / "olly-deps.opam").write_text(lock_input(depends))
        p = subprocess.run([exe, "lock", "--ocaml-version=5.4.1"], cwd=tmp,
                           env=switches.tools_env(), capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(
                "re-locking olly's dependencies failed; a new dependency may "
                "need a dune overlay in src/running/olly/overlays:\n{}".format(
                    (p.stdout + p.stderr).strip()[-3000:]))
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(Path(tmp) / "olly-deps.opam.locked", out)


def lock_for(depends: List[str]) -> Path:
    """The lockfile for olly's ``depends``: the committed one when they match."""
    if depends == _committed_depends():
        return LOCK
    h = _sha256("\n".join(depends).encode())[:12]
    cached = cache_dir() / "locks" / "{}.opam.locked".format(h)
    if not cached.exists():
        logging.info("olly's dependencies differ from the ones running-ng "
                     "locked; re-locking into %s", cached)
        _relock(depends, cached)
    return cached


# --- sources -----------------------------------------------------------------

def _duniverse_dirs(lock_text: str) -> List[Tuple[str, str, str]]:
    """(url, dir, sha256) of every vendored archive in an opam-monorepo lock."""
    m = re.search(r"x-opam-monorepo-duniverse-dirs:\s*\[(.*?)^\]", lock_text, re.S | re.M)
    if not m:
        raise RuntimeError("lockfile has no x-opam-monorepo-duniverse-dirs")
    out = []
    for url, d, hashes in re.findall(r'\[\s*"([^"]+)"\s*"([^"]+)"\s*\[(.*?)\]\s*\]',
                                     m.group(1), re.S):
        sha = re.search(r'"sha256=([0-9a-f]{64})"', hashes)
        if not sha or url.startswith("git+"):
            raise RuntimeError("cannot fetch {} ({}): only archives with a "
                               "sha256 are supported".format(d, url))
        out.append((url, d, sha.group(1)))
    return out


def _fetch(url: str, sha256: str) -> bytes:
    cached = cache_dir() / "downloads" / sha256
    if cached.exists():
        return cached.read_bytes()
    logging.info("downloading %s", url)
    with urllib.request.urlopen(url, timeout=300) as r:
        data = r.read()
    if _sha256(data) != sha256:
        raise RuntimeError("{}: sha256 mismatch".format(url))
    cached.parent.mkdir(parents=True, exist_ok=True)
    tmp = cached.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, cached)
    return data


def _extract(data: bytes, dest: Path, strip: int = 1) -> None:
    """Extract a tarball into dest, dropping its first ``strip`` path components."""
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        for m in tf.getmembers():
            parts = Path(m.name).parts[strip:]
            if not parts or ".." in parts or m.issym() or m.islnk():
                if m.issym() or m.islnk():
                    logging.debug("skipping link %s", m.name)
                continue
            m.name = str(Path(*parts))
            tf.extract(m, dest, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))


def _olly_tree_from_commit(commit: str, dest: Path) -> None:
    git = cache_dir() / "git"
    if not (git / "HEAD").exists():
        git.mkdir(parents=True, exist_ok=True)
        _git("init", "--bare", "-q", str(git))
    try:
        _git("cat-file", "-e", commit + "^{commit}", cwd=git)
    except subprocess.CalledProcessError:
        logging.info("fetching runtime_events_tools %s", commit)
        _git("fetch", "-q", "--depth", "1", REPO, commit, cwd=git)
    archive = subprocess.run(["git", "archive", "--format=tar", commit], cwd=git,
                             check=True, capture_output=True).stdout
    _extract(archive, dest, strip=0)


def _olly_dir_files(path: Path) -> List[str]:
    files = _git("ls-files", "-co", "--exclude-standard", "-z", cwd=path)
    return sorted(f for f in files.split("\0")
                  if f and not f.startswith(("_build/", "duniverse/")))


def _apply_patch(patch: Path, tree: Path) -> None:
    p = subprocess.run(["patch", "-p1", "--forward", "--batch", "-s", "-i", str(patch)],
                       cwd=tree, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            "{} no longer applies to this olly; update or drop it:\n{}".format(
                patch.name, (p.stdout + p.stderr).strip()))


def _inputs_hash(lock: Path) -> str:
    """What else, besides olly itself, goes into a prepared tree."""
    h = hashlib.sha256(lock.read_bytes())
    h.update(str(PREPARE_VERSION).encode())
    for p in sorted(PATCHES.glob("*.patch")) + sorted(OVERLAYS.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(HERE)).encode())
            h.update(p.read_bytes())
    h.update(json.dumps(OVERLAY_SOURCES, sort_keys=True).encode())
    return h.hexdigest()[:12]


class Source:
    """The olly source this run builds: a commit, or the OLLY_DIR working tree."""

    def __init__(self) -> None:
        olly_dir = os.environ.get(DIR_ENV_VAR)
        commit = os.environ.get(COMMIT_ENV_VAR)
        if olly_dir and commit:
            raise RuntimeError("set {} or {}, not both".format(DIR_ENV_VAR, COMMIT_ENV_VAR))
        self.dir = Path(olly_dir).resolve() if olly_dir else None
        self.commit = commit or (None if olly_dir else COMMIT)
        if self.commit and not _SHA_RE.match(self.commit):
            raise RuntimeError("{}={} must be a full 40-character commit SHA".format(
                COMMIT_ENV_VAR, self.commit))

    def _opam_text(self) -> str:
        if self.dir:
            return (self.dir / "runtime_events_tools.opam").read_text()
        with tempfile.TemporaryDirectory() as tmp:
            _olly_tree_from_commit(self.commit, Path(tmp))
            return (Path(tmp) / "runtime_events_tools.opam").read_text()

    def describe(self) -> Dict[str, str]:
        if self.dir:
            return {"olly_dir": str(self.dir), "olly_head": _git("rev-parse", "HEAD", cwd=self.dir)}
        return {"olly_commit": self.commit}

    def key(self, lock: Path) -> str:
        if self.dir:
            h = hashlib.sha256()
            for f in _olly_dir_files(self.dir):
                h.update(f.encode() + b"\0")
                h.update((self.dir / f).read_bytes())
            return "dir-{}-{}".format(h.hexdigest()[:12], _inputs_hash(lock))
        return "{}-{}".format(self.commit[:12], _inputs_hash(lock))

    def _populate(self, dest: Path) -> None:
        if self.dir:
            for f in _olly_dir_files(self.dir):
                (dest / f).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.dir / f, dest / f)
        else:
            _olly_tree_from_commit(self.commit, dest)

    def prepare(self) -> Tuple[str, Path]:
        """(key, tree): olly with its vendored dependencies, patched, ready to build."""
        lock = lock_for(parse_depends(self._opam_text()))
        k = self.key(lock)
        tree = cache_dir() / "src" / k
        lk = opam_roots.RootLock(cache_dir() / "src" / "{}.lock".format(k))
        lk.acquire(exclusive=True)
        try:
            if (tree / MARKER).exists():
                return k, tree
            if tree.exists():
                shutil.rmtree(tree)
            logging.info("preparing olly sources in %s", tree)
            tree.mkdir(parents=True)
            self._populate(tree)
            duniverse = tree / "duniverse"
            for url, d, sha in _duniverse_dirs(lock.read_text()):
                _extract(_fetch(url, sha), duniverse / d)
            for name, (url, sha) in OVERLAY_SOURCES.items():
                d = duniverse / name
                _extract(_fetch(url, sha), d)
                for p in d.iterdir():
                    if p.name != "src":
                        shutil.rmtree(p) if p.is_dir() else p.unlink()
                for f in (OVERLAYS / name).rglob("*"):
                    if f.is_file():
                        target = d / f.relative_to(OVERLAYS / name)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy(f, target)
            # as opam-monorepo writes it: vendored code's warnings are not errors
            (duniverse / "dune").write_text("(vendored_dirs *)\n")
            for patch in sorted(PATCHES.glob("*.patch")):
                _apply_patch(patch, tree)
            (tree / MARKER).write_text(json.dumps(
                dict(self.describe(), lock=str(lock)), indent=2) + "\n")
            return k, tree
        finally:
            lk.release()


# --- per-runtime builds --------------------------------------------------------

def _build_dir(runtime, key: str) -> Path:
    root = runtime.get_root() if hasattr(runtime, "get_root") else None
    if root is not None:
        return root.path / "olly" / key
    # executable mode: no root to keep it in
    h = _sha256(str(runtime.executable).encode())[:12]
    return cache_dir() / "builds" / h / key


def build(runtime, key: str, tree: Path) -> Path:
    """olly built with ``runtime``'s compiler; returns the binary."""
    bdir = _build_dir(runtime, key)
    exe = bdir / "default" / "bin" / "olly.exe"
    bdir.parent.mkdir(parents=True, exist_ok=True)
    lk = opam_roots.RootLock(bdir.parent / "{}.lock".format(key))
    lk.acquire(exclusive=True)
    try:
        log = bdir.parent / "{}.build.log".format(key)
        logging.info("building olly for runtime %s in %s", runtime.name, bdir)
        with log.open("w") as fh:
            p = subprocess.run(
                ["dune", "build", "--root", str(tree), "--build-dir", str(bdir),
                 "--profile", "release", "./bin/olly.exe"],
                env=runtime.get_switch_env(), stdout=fh, stderr=subprocess.STDOUT)
        if p.returncode != 0 or not exe.is_file():
            tail = log.read_text().splitlines()[-30:]
            raise RuntimeError("building olly for runtime {} failed (log: {}):\n{}".format(
                runtime.name, log, "\n".join(tail)))
        return exe
    finally:
        lk.release()


#: runtime name -> olly binary, for this process.
_binaries: Dict[str, str] = {}


def explicit_binary() -> Optional[str]:
    """``OLLY_BIN``: one olly for every runtime (a file, or a directory holding ``olly``)."""
    v = os.environ.get(BIN_ENV_VAR)
    if not v:
        return None
    p = Path(v)
    exe = p / "olly" if p.is_dir() else p
    if not os.access(exe, os.X_OK):
        raise RuntimeError("{}={}: no executable olly there".format(BIN_ENV_VAR, v))
    return str(exe)


def ensure(runtimes) -> Dict[str, str]:
    """Build olly for each runtime; any failure raises before a benchmark runs."""
    explicit = explicit_binary()
    if explicit:
        logging.info("using olly from %s=%s for every runtime", BIN_ENV_VAR, explicit)
        for rt in runtimes:
            _binaries[rt.name] = explicit
        return dict(_binaries)
    src = Source()
    key, tree = src.prepare()
    for rt in runtimes:
        if rt.name not in _binaries:
            _binaries[rt.name] = str(build(rt, key, tree))
    return dict(_binaries)


def binary_for(runtime) -> str:
    """The olly to attach to a benchmark on ``runtime`` ("olly" from PATH if none was built)."""
    return _binaries.get(runtime.name) or explicit_binary() or "olly"


def version() -> Optional[str]:
    """What olly this run used, for the contract's tool_versions."""
    explicit = explicit_binary()
    if explicit:
        for anc in Path(os.path.realpath(explicit)).parents:
            if (anc / ".git").exists():
                try:
                    return _git("describe", "--tags", "--always", "--dirty", cwd=anc)
                except subprocess.CalledProcessError:
                    return None
        return None
    src = Source()
    if src.dir:
        return _git("describe", "--always", "--dirty", cwd=src.dir)
    return src.commit


# --- maintenance ---------------------------------------------------------------

def relock_committed(commit: str) -> None:
    """Re-lock for ``commit`` and rewrite the committed lock (an olly bump)."""
    with tempfile.TemporaryDirectory() as tmp:
        _olly_tree_from_commit(commit, Path(tmp))
        depends = parse_depends((Path(tmp) / "runtime_events_tools.opam").read_text())
    _relock(depends, LOCK, check_overlays=False)
    DEPENDS.write_text("\n".join(depends) + "\n")
    print("Locked olly {}; set COMMIT in {} to it.".format(commit, __file__))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m running.olly")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepare", help="prepare olly's sources (no build)")
    lk = sub.add_parser("lock", help="re-lock olly's dependencies for a new COMMIT")
    lk.add_argument("commit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.cmd == "prepare":
        print(Source().prepare()[1])
    elif args.cmd == "lock":
        relock_committed(args.commit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
