"""One opam root per compiler identity.

Each OCaml runtime gets its own opam root under ``$RUNNING_OPAM_ROOTS``, named
after everything that determines what gets built: the compiler's git SHA, the
build settings, and the opam-repository commit the root is initialised from.
A root whose identity matches is reused; anything else gets a new root, so
runtimes never share repositories, pins or global options, and nothing guesses
whether a leftover switch is still what the config asks for.

A root cannot be built elsewhere and moved into place (compilers record their
install path), so it is built in place under an exclusive lock, and
``root.json`` is written last: a root without it is incomplete and rebuilt.
Runs hold a shared lock on every root they use.

Standard library only: ``python3 -m running.opam_roots list|gc``.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOTS_ENV_VAR = "RUNNING_OPAM_ROOTS"

#: opam-repository commit every root is initialised from. Bump by PR, like any
#: other pin; a runtime can override it with ``opam_repository:``.
OPAM_REPOSITORY_URL = "https://github.com/ocaml/opam-repository"
OPAM_REPOSITORY_COMMIT = "daca28e1fae6100f9052f4cf4a8b0899fe175b57"

#: The single switch inside a runtime root.
SWITCH = "runtime"

RECORD = "root.json"
LAST_USED = "last-used"
DOWNLOAD_CACHE = "download-cache"
#: running-ng's own root (tools and olly switches), not a runtime root.
TOOLS_ROOT = "running-ng"

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def roots_dir() -> Path:
    override = os.environ.get(ROOTS_ENV_VAR)
    if override:
        return Path(override)
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(cache) / "running-ng" / "opam-roots"


def _git_ls_remote(repo: str, patterns: List[str]) -> str:
    return subprocess.run(
        ["git", "ls-remote", repo] + patterns,
        capture_output=True, text=True, check=True,
    ).stdout


def resolve_ref(repo: str, ref: str) -> str:
    """The commit SHA ``ref`` (a SHA, tag or branch) names in ``repo``."""
    if _SHA_RE.match(ref):
        return ref
    out = _git_ls_remote(repo, [
        "refs/tags/{}^{{}}".format(ref), "refs/tags/{}".format(ref),
        "refs/heads/{}".format(ref)])
    found = {}
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        found[name] = sha
    # A peeled tag is the commit; an unpeeled one may be a tag object.
    for name in ("refs/tags/{}^{{}}".format(ref), "refs/tags/{}".format(ref),
                 "refs/heads/{}".format(ref)):
        if name in found:
            return found[name]
    if re.match(r"^[0-9a-f]{7,39}$", ref):
        raise ValueError(
            "Abbreviated commit {} in {}: give the full 40-character SHA."
            .format(ref, repo))
    raise ValueError("{} has no tag or branch named {}".format(repo, ref))


def identity(kind: str, repo: str, ref: str, *,
             configure_args: Optional[List[str]] = None,
             dune_version: Optional[str] = None,
             relocatable: bool = False,
             opam_repository: Optional[str] = None) -> Dict[str, Any]:
    """Everything that determines a root's contents, with refs resolved to SHAs."""
    return {
        "kind": kind,
        "repo": repo,
        "ref": ref,
        "sha": resolve_ref(repo, ref),
        "configure_args": list(configure_args or []),
        "dune_version": dune_version,
        "relocatable": relocatable,
        "opam_repository": opam_repository or OPAM_REPOSITORY_COMMIT,
    }


def key(ident: Dict[str, Any]) -> str:
    """Directory name: readable prefix, then a hash of the whole identity."""
    digest = hashlib.sha256(
        json.dumps(ident, sort_keys=True).encode()).hexdigest()[:12]
    return "{}-{}-{}".format(ident["kind"].lower(), ident["sha"][:12], digest)


class Root:
    def __init__(self, path: Path):
        self.path = path

    @property
    def record_path(self) -> Path:
        return self.path / RECORD

    def complete(self) -> bool:
        return self.record_path.is_file()

    def record(self) -> Dict[str, Any]:
        return json.loads(self.record_path.read_text())

    def env(self, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        env = dict(os.environ if base is None else base)
        env["OPAMROOT"] = str(self.path)
        env.pop("OPAMSWITCH", None)
        return env

    def switch_prefix(self) -> Path:
        return self.path / SWITCH

    def touch(self) -> None:
        (self.path / LAST_USED).write_text(_now() + "\n")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sandboxing_available() -> bool:
    return sys.platform.startswith("linux") and shutil.which("bwrap") is not None


def init_root(opam: str, root: Root, opam_repository: str) -> None:
    """``opam init`` the root from the pinned opam-repository commit, sharing
    the download cache with every other root."""
    cmd = [opam, "init", "--bare", "--no-setup", "--yes",
           "default", "git+{}#{}".format(OPAM_REPOSITORY_URL, opam_repository)]
    if not _sandboxing_available():
        cmd.insert(2, "--disable-sandboxing")
    subprocess.run(cmd, check=True, env=root.env())
    shared = root.path.parent / DOWNLOAD_CACHE
    shared.mkdir(parents=True, exist_ok=True)
    own = root.path / DOWNLOAD_CACHE
    if own.is_symlink():
        return
    if own.exists():
        shutil.rmtree(own)
    own.symlink_to(shared)


class RootLock:
    """flock on a file beside the root, so it outlives a rebuild of the root."""

    def __init__(self, path: Path):
        self.path = path
        self.fh: Optional[Any] = None

    def _open(self) -> Any:
        if self.fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = self.path.open("a+")
        return self.fh

    def acquire(self, exclusive: bool) -> None:
        fh = self._open()
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(fh.fileno(), mode | fcntl.LOCK_NB)
        except OSError:
            logging.info("Waiting for %s (another run holds it)", self.path)
            fcntl.flock(fh.fileno(), mode)

    def try_exclusive(self) -> bool:
        fh = self._open()
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.release()
            return False

    def release(self) -> None:
        if self.fh is not None:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            self.fh.close()
            self.fh = None


def _locks(base: Path, k: str):
    # Separate locks: building must not wait for other runs' whole use of a
    # complete root, and two runs must never both hold one while upgrading.
    return (RootLock(base / "{}.create.lock".format(k)),
            RootLock(base / "{}.use.lock".format(k)))


#: Use locks held for the rest of the process.
_held: List[RootLock] = []


def ensure(ident: Dict[str, Any], build: Callable[[Root], None],
           opam: str = "opam", base: Optional[Path] = None) -> Root:
    """The complete root for ``ident``, building it with ``build`` if needed.

    Returns holding a shared lock on it until :func:`release_all`.
    """
    base = base or roots_dir()
    k = key(ident)
    root = Root(base / k)
    create, use = _locks(base, k)
    while True:
        if not root.complete():
            create.acquire(exclusive=True)
            try:
                if not root.complete():
                    if root.path.exists():
                        logging.warning("Rebuilding incomplete opam root %s", root.path)
                        shutil.rmtree(root.path)
                    logging.info("Creating opam root %s", root.path)
                    init_root(opam, root, ident["opam_repository"])
                    build(root)
                    record = dict(ident, created=_now())
                    root.record_path.write_text(json.dumps(record, indent=2) + "\n")
            finally:
                create.release()
        use.acquire(exclusive=False)
        # gc may have removed it between the check and the lock.
        if root.complete():
            break
        use.release()
    logging.info("Using opam root %s", root.path)
    root.touch()
    _held.append(use)
    return root


def release_all() -> None:
    while _held:
        _held.pop().release()


def list_roots(base: Optional[Path] = None) -> List[Dict[str, Any]]:
    base = base or roots_dir()
    out = []
    if not base.is_dir():
        return out
    for p in sorted(base.iterdir()):
        if not p.is_dir() or p.name in (DOWNLOAD_CACHE, TOOLS_ROOT):
            continue
        root = Root(p)
        entry: Dict[str, Any] = {"key": p.name, "path": str(p),
                                 "complete": root.complete()}
        if root.complete():
            entry.update(root.record())
        used = p / LAST_USED
        entry["last_used"] = used.read_text().strip() if used.exists() else None
        out.append(entry)
    return out


def gc(unused_for_days: float, base: Optional[Path] = None,
       dry_run: bool = False) -> List[str]:
    """Remove roots not used for ``unused_for_days``, skipping any in use."""
    base = base or roots_dir()
    cutoff = time.time() - unused_for_days * 86400
    removed = []
    for entry in list_roots(base):
        p = Path(entry["path"])
        used = p / LAST_USED
        mtime = used.stat().st_mtime if used.exists() else p.stat().st_mtime
        if mtime > cutoff:
            continue
        create, use = _locks(base, entry["key"])
        if not use.try_exclusive():
            logging.info("Skipping %s: in use", p)
            continue
        if not create.try_exclusive():
            use.release()
            logging.info("Skipping %s: being built", p)
            continue
        try:
            if not dry_run:
                shutil.rmtree(p)
            removed.append(str(p))
        finally:
            create.release()
            use.release()
    return removed


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m running.opam_roots")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list the opam roots and what each was built from")
    g = sub.add_parser("gc", help="remove roots not used recently")
    g.add_argument("--unused-for", type=float, required=True, metavar="DAYS")
    g.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "list":
        for e in list_roots():
            print("{key}  {state}  last used {used}  {kind} {repo}@{ref} ({sha})".format(
                key=e["key"], state="complete" if e["complete"] else "INCOMPLETE",
                used=e.get("last_used") or "never", kind=e.get("kind", "?"),
                repo=e.get("repo", "?"), ref=e.get("ref", "?"),
                sha=(e.get("sha") or "?")[:12]))
        return 0
    for p in gc(args.unused_for, dry_run=args.dry_run):
        print("{} {}".format("would remove" if args.dry_run else "removed", p))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
