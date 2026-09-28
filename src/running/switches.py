"""running-ng's own opam switches (tools and olly), in running-ng's own opam
root (``$RUNNING_OPAM_ROOTS/running-ng``), separate from the per-runtime roots
runtime.py provisions. Two switches because olly needs
cmdliner >= 2.0 and opam-compiler pins cmdliner < 2.0. A switch is rebuilt
when its observed identity (package versions, olly checkout SHA) no longer
matches what was recorded at creation.

Standard library only, so it can run before running-ng's dependencies are
installed: `python3 -m running.switches --help`.
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

from running import opam_roots

#: Overrides where the machine-local state file (what was built, from what) lives.
STATE_ENV_VAR = "RUNNING_NG_STATE_DIR"

#: Where the olly checkout lives.
OLLY_DIR_ENV_VAR = "OLLY_DIR"

TOOLS_SWITCH = "running-ng-tools"
OLLY_SWITCH = "running-ng-olly"

DEFAULT_COMPILER = "5.4.0"

#: Declaration of each switch; the state file records what came of it.
SWITCHES: Dict[str, Dict] = {
    TOOLS_SWITCH: {
        "purpose": "build tools and the opam-compiler plugin",
        "packages": ["dune", "ocamlfind", "opam-compiler"],
        # A change in any of these versions triggers a rebuild.
        "identity_packages": ["ocaml", "dune", "ocamlfind", "opam-compiler"],
        "source": None,
        # `flags: plugin` packages must be linked into $(opam var root)/plugins/bin
        # or `opam compiler create` cannot resolve them.
        "registers_plugin": "opam-compiler",
        # OxCaml support (ocaml-opam/opam-compiler#42); drop once it is released.
        "pins": {
            "opam-compiler": "git+https://github.com/udesou/opam-compiler.git"
                             "#9b9ca6184e8b04f539f1c6308e5dc05a5a6b39d3",
        },
    },
    OLLY_SWITCH: {
        "purpose": "olly (runtime_events_tools), which needs cmdliner >= 2.0",
        # Resolved --deps-only from olly's own opam file.
        "packages": [],
        "identity_packages": ["ocaml", "cmdliner"],
        "source": OLLY_DIR_ENV_VAR,
        "registers_plugin": None,
        "pins": {},
    },
}


def tools_root() -> str:
    return str(opam_roots.roots_dir() / opam_roots.TOOLS_ROOT)


def tools_env() -> Dict[str, str]:
    """The environment for every opam command here: the tools root, never the user's."""
    return opam_roots.Root(opam_roots.roots_dir() / opam_roots.TOOLS_ROOT).env()


def _ensure_tools_root(opam: str) -> None:
    root = opam_roots.Root(opam_roots.roots_dir() / opam_roots.TOOLS_ROOT)
    if (root.path / "config").exists():
        return
    logging.info("initialising running-ng's opam root %s", root.path)
    opam_roots.init_root(opam, root, opam_roots.OPAM_REPOSITORY_COMMIT)


def _run(cmd: List[str], check: bool = True) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, env=tools_env())
    if check and p.returncode != 0:
        raise RuntimeError("{} failed ({}): {}".format(
            " ".join(cmd), p.returncode, p.stderr.strip()))
    return p.stdout.strip()


#: Explicit opam binary, for installers whose user-local opam is not on PATH yet.
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
    """State dir: RUNNING_NG_STATE_DIR, else the tools root, whose switches the
    state describes."""
    return os.environ.get(STATE_ENV_VAR) or tools_root()


def state_path() -> str:
    return os.path.join(state_dir(), "switches.json")


def load_state() -> Dict:
    try:
        with open(state_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        # missing or corrupt state means nothing is known; everything gets rebuilt
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


def _pins(opam: str, switch: str) -> Dict[str, str]:
    """Pinned package name -> pin target, as `opam pin list` reports them."""
    out = _run([opam, "pin", "list", "--switch", switch], check=False)
    pins = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            pins[parts[0].split(".")[0]] = parts[2]
    return pins


def source_dir(spec: Dict) -> Optional[str]:
    """The checkout a switch is built from, or None if it has no source.

    Shared by _source_sha and ensure(): if they resolved it separately, a
    machine with two checkouts would rebuild the switch on alternate runs.
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
    """The switch's current identity (package versions, source SHA), or None if absent."""
    if not switch_exists(opam, name):
        return None
    spec = SWITCHES[name]
    identity: Dict[str, Optional[str]] = dict(
        _package_versions(opam, name, spec["identity_packages"]))
    # A pinned package keeps its version number when the pin target moves.
    for pkg, target in _pins(opam, name).items():
        identity["pin:" + pkg] = target
    sha = _source_sha(spec)
    if sha is not None:
        identity["source_sha"] = sha
    return identity


def missing_packages(opam: str, name: str) -> List[str]:
    """Declared packages of `name` that opam says are not installed in it."""
    declared = SWITCHES[name].get("packages") or []
    if not declared:
        return []
    installed = _package_versions(opam, name, declared)
    return [p for p in declared if not installed.get(p)]


def pin_drift(name: str, observed: Dict) -> List[str]:
    """Declared pins of `name` that `observed` does not have."""
    return [pkg for pkg, target in SWITCHES[name].get("pins", {}).items()
            if observed.get("pin:" + pkg) != target]


def pin_commands(opam: str, name: str, pkgs: List[str],
                 install: bool) -> List[List[str]]:
    pins = SWITCHES[name].get("pins", {})
    return [[opam, "pin", "add", "--switch", name, "--yes"]
            + ([] if install else ["--no-action"]) + [pkg, pins[pkg]]
            for pkg in pkgs]


def plan(opam: str, name: str) -> str:
    """One of 'create', 'rebuild', 'repair', 'adopt' or 'ok'."""
    observed = observe(opam, name)
    if observed is None:
        return "create"
    recorded = load_state().get("switches", {}).get(name, {}).get("identity")

    # Check contents against the declaration, not only against the recorded
    # identity: `adopt` records whatever it finds, damage included, so a
    # switch missing a declared package would otherwise report 'ok' forever.
    missing = missing_packages(opam, name)
    if missing:
        # Repair (cheap) unless something other than the missing packages also
        # drifted: repair re-records afterwards and would adopt that drift.
        others = [k for k in set(recorded or {}) | set(observed)
                  if k not in missing and (recorded or {}).get(k) != observed.get(k)]
        return "rebuild" if (recorded is not None and others) else "repair"

    drift = pin_drift(name, observed)
    if recorded is None:
        # exists, not built by us, complete: adopt rather than destroy
        return "repair" if drift else "adopt"
    # Pins are re-applied in place; anything else that moved means a rebuild.
    moved = [k for k in set(recorded) | set(observed)
             if not k.startswith("pin:") and recorded.get(k) != observed.get(k)]
    if moved:
        return "rebuild"
    return "repair" if drift or recorded != observed else "ok"


# --- creation ------------------------------------------------------------------

def _active_switch(opam: str) -> Optional[str]:
    out = _run([opam, "switch", "show"], check=False).strip()
    return out or None


def build_commands(name: str, compiler: str = DEFAULT_COMPILER) -> List[List[str]]:
    """The commands that would create `name`, without running any of them."""
    opam = "opam"
    spec = SWITCHES[name]
    cmds = [[opam, "switch", "create", name,
             "ocaml-base-compiler.{}".format(compiler), "--yes"]]
    cmds += pin_commands(opam, name, list(spec.get("pins", {})), install=False)
    if spec["packages"]:
        cmds.append([opam, "install", "--switch", name, "--yes"]
                    + spec["packages"])
    if spec["source"]:
        cmds.append([opam, "install", "--switch", name, "--deps-only", "--yes", "."])
    return cmds


def _ensure_cmake_depext_bypass(opam: str) -> None:
    """Tell opam ``conf-cmake``'s depext is satisfied when cmake is on PATH.

    A user-local cmake is invisible to dpkg/pkg, so opam's depext check would
    prompt and hang a headless sweep. Global setting; idempotent."""
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
    """Make `name` exist and match its declaration; returns the action taken.
    Leaves the previously active switch selected.
    """
    opam = find_opam()
    if not dry_run:
        _ensure_tools_root(opam)
    action = plan(opam, name)
    if action == "ok":
        logging.info("opam switch '%s' is up to date", name)
        # The plugin link lives outside the switch; check it anyway.
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

    if action == "repair":
        # May downgrade packages (opam-compiler pins cmdliner < 2.0); that is
        # correct for this switch.
        missing = missing_packages(opam, name)
        drift = pin_drift(name, observe(opam, name) or {})
        if missing:
            logging.warning(
                "opam switch '%s' is missing packages it declares: %s. Installing "
                "them; this may change the versions of packages that depend on "
                "them.", name, ", ".join(missing))
        if drift:
            logging.warning("opam switch '%s' does not pin %s as declared; "
                            "re-pinning", name, ", ".join(drift))
        cmds = pin_commands(opam, name, drift, install=True)
        if missing:
            cmds.append([opam, "install", "--switch", name, "--yes"] + missing)
        if dry_run:
            for cmd in cmds:
                logging.info("DRY RUN: %s", " ".join(cmd))
            return "repair"
        previous = _active_switch(opam)
        try:
            for cmd in cmds:
                subprocess.run(cmd, check=True, env=tools_env())
            _register_plugin(opam, name)
            _record(opam, name)
        finally:
            if previous and _active_switch(opam) != previous:
                _run([opam, "switch", "set", previous], check=False)
        return "repair"

    previous = _active_switch(opam)
    try:
        if action == "rebuild":
            # Name the keys that moved: a rebuild recompiles a compiler, and
            # the usual cause is $OLLY_DIR pointing at a different checkout.
            recorded = (load_state().get("switches", {})
                        .get(name, {}).get("identity") or {})
            observed = observe(opam, name) or {}
            changed = ["{}: {} -> {}".format(k, recorded.get(k, "absent"),
                                             observed.get(k, "absent"))
                       for k in sorted(set(recorded) | set(observed))
                       if recorded.get(k) != observed.get(k)]
            logging.warning(
                "opam switch '%s' no longer matches what it was built from; "
                "rebuilding it (%s)", name, "; ".join(changed) or "no visible "
                "difference")
            if any(c.startswith("source_sha") for c in changed):
                logging.warning(
                    "  the source checkout moved: %s. If you did not intend "
                    "that, check $%s -- pointing it at a different checkout "
                    "than the previous run is what makes this rebuild happen "
                    "on every run.",
                    source_dir(SWITCHES[name]), SWITCHES[name].get("source"))
            if dry_run:
                logging.info("DRY RUN: opam switch remove %s --yes", name)
            else:
                _run([opam, "switch", "remove", name, "--yes"], check=False)

        cwd = None
        spec = SWITCHES[name]
        if spec["source"]:
            # olly's hdr_histogram dep needs cmake
            if not dry_run:
                _ensure_cmake_depext_bypass(opam)
            cwd = source_dir(spec)
        for cmd in build_commands(name, compiler):
            if dry_run:
                logging.info("DRY RUN: %s%s", " ".join(cmd),
                             " (in {})".format(cwd) if cwd and cmd[-1] == "." else "")
                continue
            subprocess.run(cmd, check=True, env=tools_env(),
                           cwd=cwd if cmd[-1] == "." else None)
        if not dry_run:
            _register_plugin(opam, name)
            _record(opam, name)
    finally:
        if previous and not dry_run and _active_switch(opam) != previous:
            _run([opam, "switch", "set", previous], check=False)
            logging.info("restored the active opam switch to '%s'", previous)
    return action


def _register_plugin(opam: str, name: str) -> None:
    """Link a `flags: plugin` package into $(opam var root)/plugins/bin.

    opam resolves plugins from there, not from the installing switch; without
    the link `opam compiler create` fails with "unknown command 'compiler'".
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
        # external/local switches are not under the opam root; use an absolute target
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
    parser.add_argument("action", choices=["status", "ensure", "path", "root"])
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
    if args.action == "root":
        print(tools_root())
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
