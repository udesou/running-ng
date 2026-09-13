"""Infrastructure opam switches: declare them, create them, cache them.

running-ng needs opam switches of its own, separate from the per-runtime
switches `runtime.py` provisions for benchmarks. This module owns them.

Three rules shape the design:

* **Never borrow a switch.** The sweep wrapper used to scan `opam switch list`
  for the first one containing dune, which picked a local switch on one
  machine and the olly switch on another. A switch we did not build has
  unknown contents, so we declare what we need and build it.
* **Two switches, not one.** olly needs cmdliner >= 2.0 while every published
  opam-compiler pins cmdliner < 2.0. Installing either into the other's switch
  corrupts it, which has happened twice. They are declared separately here so
  that cannot be expressed.
* **Put the active switch back.** Creating a switch changes what opam
  considers current, which is the user's setting, not ours.

Invalidation is by observed identity: what a switch was built from is recorded
when it is created, and a switch whose observation no longer matches is
rebuilt. For the olly switch that identity includes the git SHA of the olly
checkout, so moving the checkout rebuilds olly, which is the case that has
actually bitten people.

POLICY NOTE: the bench service is expected to key its own switch cache on
declared VERSIONS rather than observed SHAs, because it provisions from a
manifest rather than from whatever is checked out locally. This module is the
local counterpart and deliberately uses SHAs, since locally the checkout is
the source of truth. If the two are ever unified, this is the thing to change:
replace `observe()` with a function of the declaration alone.

Standard library only, so this can run before running-ng's dependencies are
installed. `python3 -m running.switches --help`.

STILL DUPLICATED: install_deps_{linux,macos,freebsd}.sh create these same two
switches inline, and that duplication is exactly how the wrapper and the
installers drifted apart in the first place. They should delegate here,
keeping only the parts this does not own (the opam binary, the olly dune
build, the benchmarks clone). Not done yet because the FreeBSD installer was
verified on hardware in its current form and changing it means re-verifying.
"""
import argparse
import datetime
import json
import logging
import os
import shutil
import subprocess
import sys
from typing import Dict, List, Optional

#: State file recording what was built and from what. Machine-local, so NOT in
#: the repo: it would show up in `git status`, get committed by accident, and
#: be wrong on the next machine. The declaration below is the versioned half.
STATE_ENV_VAR = "RUNNING_NG_STATE_DIR"

#: Where the olly checkout lives, matching install_deps_*.sh.
OLLY_DIR_ENV_VAR = "OLLY_DIR"

TOOLS_SWITCH = "running-ng-tools"
OLLY_SWITCH = "running-ng-olly"

DEFAULT_COMPILER = "5.4.0"

#: What each switch is for and what goes in it. The versioned half of the
#: state: this is the declaration, the state file records what came of it.
SWITCHES: Dict[str, Dict] = {
    TOOLS_SWITCH: {
        "purpose": "build tools and the opam-compiler plugin",
        "packages": ["dune", "ocamlfind", "opam-compiler"],
        # Identity: the versions of what we installed, plus the compiler. If
        # any moves, something changed the switch out from under us.
        "identity_packages": ["ocaml", "dune", "ocamlfind", "opam-compiler"],
        "source": None,
        # opam-compiler declares `flags: plugin`, so it must also be linked
        # into $(opam var root)/plugins/bin or `opam compiler create` cannot
        # resolve it. See install_deps_*.sh.
        "registers_plugin": "opam-compiler",
    },
    OLLY_SWITCH: {
        "purpose": "olly (runtime_events_tools), which needs cmdliner >= 2.0",
        # Resolved from olly's own opam file rather than listed here: a
        # hand-written list is what let the cmdliner conflict through.
        "packages": [],
        "identity_packages": ["ocaml", "cmdliner"],
        "source": OLLY_DIR_ENV_VAR,
        "registers_plugin": None,
    },
}


def _run(cmd: List[str], check: bool = True) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError("{} failed ({}): {}".format(
            " ".join(cmd), p.returncode, p.stderr.strip()))
    return p.stdout.strip()


#: Explicit opam binary, for callers that installed their own. The FreeBSD
#: installer puts a user-local opam in ~/.local/bin, which may not be on PATH
#: yet at the point it calls us.
OPAM_BIN_ENV_VAR = "OPAM_BIN"


def find_opam() -> str:
    explicit = os.environ.get(OPAM_BIN_ENV_VAR)
    if explicit:
        if not os.path.isfile(explicit):
            raise RuntimeError("{}={} is not a file".format(
                OPAM_BIN_ENV_VAR, explicit))
        return explicit
    opam = shutil.which("opam") or os.path.expanduser("~/.local/bin/opam")
    if not os.path.isfile(opam):
        raise RuntimeError("opam not found on PATH, at ~/.local/bin/opam, or "
                           "via {}".format(OPAM_BIN_ENV_VAR))
    return opam


def state_dir() -> str:
    override = os.environ.get(STATE_ENV_VAR)
    if override:
        return override
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cache, "running-ng")


def state_path() -> str:
    return os.path.join(state_dir(), "switches.json")


def load_state() -> Dict:
    try:
        with open(state_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        # A missing or corrupt state file means "nothing is known", which is
        # the same as a fresh machine: everything gets rebuilt. Never fatal.
        return {"version": 1, "switches": {}}


def save_state(state: Dict) -> None:
    os.makedirs(state_dir(), exist_ok=True)
    tmp = state_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, state_path())


def switch_exists(opam: str, name: str) -> bool:
    return name in _run([opam, "switch", "list", "--short"],
                        check=False).splitlines()


def _package_versions(opam: str, switch: str, packages: List[str]) -> Dict[str, str]:
    out = _run([opam, "list", "--switch", switch, "--installed",
                "--columns=name,version", "--short"] + packages, check=False)
    versions = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            versions[parts[0]] = parts[1]
    return versions


def source_dir(spec: Dict) -> Optional[str]:
    """The checkout a switch is built from, or None if it has no source.

    Resolved in ONE place because the two callers must agree: _source_sha
    decides whether a rebuild is due, ensure() builds there, and a machine
    where they disagree rebuilds the switch on every alternate run.

    That is not hypothetical. The sweep wrapper resolves OLLY_DIR itself and
    used not to export it, so this module fell back to ~/runtime_events_tools
    -- a SECOND checkout, at a different revision. A manual sweep then observed
    one SHA and the bench agent (which does pass OLLY_DIR) observed the other,
    so each run tore down and recompiled the olly switch the other had just
    built. The fallback is kept for a machine with a single checkout in the
    default location, but it is now announced rather than silent, because a
    silent one is indistinguishable from the thrash it causes.
    """
    env_var = spec.get("source")
    if not env_var:
        return None
    path = os.environ.get(env_var)
    if path:
        return path
    fallback = os.path.expanduser("~/runtime_events_tools")
    logging.warning(
        "$%s is not set; falling back to %s. If that is not the checkout you "
        "build from, set %s -- otherwise this switch is keyed on the wrong "
        "revision and will be rebuilt on every other run.",
        env_var, fallback, env_var)
    return fallback


def _source_sha(spec: Dict) -> Optional[str]:
    """git SHA of the checkout a switch is built from, if it has one."""
    path = source_dir(spec)
    if path is None:
        return None
    try:
        return _run(["git", "-C", path, "rev-parse", "HEAD"])
    except RuntimeError:
        return None


def observe(opam: str, name: str) -> Optional[Dict]:
    """What this switch is built from right now, or None if it is absent.

    Compared against what was recorded at creation time to decide whether a
    rebuild is due. Includes the source SHA, which is the whole point for the
    olly switch: moving the checkout must rebuild olly.
    """
    if not switch_exists(opam, name):
        return None
    spec = SWITCHES[name]
    identity: Dict[str, Optional[str]] = dict(
        _package_versions(opam, name, spec["identity_packages"]))
    sha = _source_sha(spec)
    if sha is not None:
        identity["source_sha"] = sha
    return identity


def plan(opam: str, name: str) -> str:
    """One of 'create', 'rebuild', 'ok'."""
    observed = observe(opam, name)
    if observed is None:
        return "create"
    recorded = load_state().get("switches", {}).get(name, {}).get("identity")
    if recorded is None:
        # The switch exists but we did not build it, or the state was lost.
        # Adopt it rather than destroying someone's work: record what is there
        # and move on. A genuine drift is caught on the next run.
        return "adopt"
    return "ok" if recorded == observed else "rebuild"


# --- creation ------------------------------------------------------------------

def _active_switch(opam: str) -> Optional[str]:
    out = _run([opam, "switch", "show"], check=False).strip()
    return out or None


def build_commands(name: str, compiler: str = DEFAULT_COMPILER) -> List[List[str]]:
    """The commands that would create `name`, without running any of them.

    Split out so the plan is inspectable and testable without an opam root:
    creating a switch compiles a compiler, so this is the only part that can
    be exercised anywhere.
    """
    opam = "opam"
    spec = SWITCHES[name]
    cmds = [[opam, "switch", "create", name,
             "ocaml-base-compiler.{}".format(compiler), "--yes"]]
    if spec["packages"]:
        cmds.append([opam, "install", "--switch", name, "--yes"]
                    + spec["packages"])
    if spec["source"]:
        # --deps-only from the project's own opam file, never a list here.
        cmds.append([opam, "install", "--switch", name, "--deps-only", "--yes", "."])
    return cmds


def _ensure_cmake_depext_bypass(opam: str) -> None:
    """Stop opam prompting for a system ``cmake`` when a usable cmake is already
    on PATH but invisible to opam's package-manager depext check.

    olly's ``hdr_histogram`` dependency declares ``conf-cmake``, whose depext is
    the distro ``cmake`` package. On a no-sudo box cmake is often installed
    user-local (e.g. ``~/.local/bin/cmake``), which dpkg/pkg cannot see, so opam
    stops to ask whether to install it — which hangs a headless sweep. When
    cmake is on PATH we tell opam it is satisfied. Global, so it survives the
    switch rebuilds this module does, and covers every caller (the sweep wrapper
    and all install_deps_*.sh delegate here). Guarded on cmake actually being
    present, so a genuine absence (e.g. FreeBSD without ``pkg install cmake``) is
    still reported rather than masked. Idempotent."""
    if not shutil.which("cmake"):
        return
    current = _run([opam, "option", "--global", "depext-bypass"], check=False)
    if '"cmake"' in current:
        return
    logging.info("registering cmake as an already-satisfied opam depext "
                 "(usable cmake on PATH, invisible to the package-manager check)")
    _run([opam, "option", "--global", 'depext-bypass+=["cmake"]'], check=False)


def ensure(name: str, compiler: str = DEFAULT_COMPILER,
           dry_run: bool = False) -> str:
    """Make `name` exist and match its declaration. Returns the action taken.

    Always leaves the previously active switch selected: creating a switch
    changes what opam considers current, and that is the user's setting.
    """
    opam = find_opam()
    action = plan(opam, name)
    if action == "ok":
        logging.info("opam switch '%s' is up to date", name)
        # Still check the plugin link: the switch being right says nothing
        # about $(opam var root)/plugins/bin, and a missing link there blocks
        # every sweep. Idempotent, so this is cheap.
        if not dry_run:
            _register_plugin(opam, name)
        return "ok"

    if action == "adopt":
        logging.info("adopting pre-existing opam switch '%s' (not built by us)",
                     name)
        if not dry_run:
            _register_plugin(opam, name)
            _record(opam, name)
        return "adopt"

    previous = _active_switch(opam)
    try:
        if action == "rebuild":
            logging.warning(
                "opam switch '%s' no longer matches what it was built from; "
                "rebuilding it", name)
            if dry_run:
                logging.info("DRY RUN: opam switch remove %s --yes", name)
            else:
                _run([opam, "switch", "remove", name, "--yes"], check=False)

        cwd = None
        spec = SWITCHES[name]
        if spec["source"]:
            # olly's hdr_histogram dep needs cmake; keep opam's depext check
            # from hanging on a user-local cmake it cannot see. See the helper.
            if not dry_run:
                _ensure_cmake_depext_bypass(opam)
            cwd = source_dir(spec)
        for cmd in build_commands(name, compiler):
            if dry_run:
                logging.info("DRY RUN: %s%s", " ".join(cmd),
                             " (in {})".format(cwd) if cwd and cmd[-1] == "." else "")
                continue
            subprocess.run(cmd, check=True,
                           cwd=cwd if cmd[-1] == "." else None)
        if not dry_run:
            _register_plugin(opam, name)
            _record(opam, name)
    finally:
        # Put it back even if the build failed partway.
        if previous and not dry_run and _active_switch(opam) != previous:
            _run([opam, "switch", "set", previous], check=False)
            logging.info("restored the active opam switch to '%s'", previous)
    return action


def _register_plugin(opam: str, name: str) -> None:
    """Link a `flags: plugin` package into $(opam var root)/plugins/bin.

    opam resolves plugins from there, not from the switch that installed them,
    so without this `opam compiler create` fails with "unknown command
    'compiler'". Mirrors install_deps_*.sh; see its comment for why the
    tempting one-liner (`opam install opam-compiler` with no --switch) must
    not be used.
    """
    plugin = SWITCHES[name].get("registers_plugin")
    if not plugin:
        return
    if not _package_versions(opam, name, [plugin]).get(plugin):
        logging.warning(
            "'%s' is not installed in switch '%s'; not registering the plugin",
            plugin, name)
        return
    root = _run([opam, "var", "root"])
    plugin_bin = os.path.join(root, "plugins", "bin")
    os.makedirs(plugin_bin, exist_ok=True)
    link = os.path.join(plugin_bin, plugin)
    target = os.path.join("..", "..", name, "bin", plugin)
    if os.path.islink(link) or os.path.exists(link):
        os.remove(link)
    os.symlink(target, link)
    if not os.access(link, os.X_OK):
        # An external or local switch does not live under the opam root, so
        # the relative form does not resolve; fall back to absolute.
        os.remove(link)
        os.symlink(os.path.join(_run([opam, "var", "bin", "--switch", name]),
                                plugin), link)
    logging.info("registered opam plugin '%s' from switch '%s'", plugin, name)


def _record(opam: str, name: str) -> None:
    state = load_state()
    state.setdefault("switches", {})[name] = {
        "created_at": datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "identity": observe(opam, name),
    }
    save_state(state)


def status() -> List[Dict]:
    opam = find_opam()
    rows = []
    for name in SWITCHES:
        rows.append({
            "switch": name,
            "purpose": SWITCHES[name]["purpose"],
            "plan": plan(opam, name),
            "observed": observe(opam, name),
            "recorded": load_state().get("switches", {}).get(name),
        })
    return rows


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m running.switches",
        description="Declare, create and cache running-ng's own opam switches.")
    parser.add_argument("action", choices=["status", "ensure", "path"])
    parser.add_argument("--switch", action="append", dest="switches",
                        choices=sorted(SWITCHES),
                        help="limit to this switch (repeatable); default all")
    parser.add_argument("--compiler", default=DEFAULT_COMPILER)
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be done, change nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.action == "path":
        print(state_path())
        return 0
    names = args.switches or list(SWITCHES)
    if args.action == "status":
        for row in status():
            if row["switch"] not in names:
                continue
            print("{:<20} {:<8} {}".format(
                row["switch"], row["plan"], row["purpose"]))
            print("  observed: {}".format(row["observed"]))
            created = (row["recorded"] or {}).get("created_at", "-")
            print("  built at: {}".format(created))
        return 0
    for name in names:
        ensure(name, compiler=args.compiler, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
