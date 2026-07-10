"""`running adapt` — legacy → data-contract adaptation (3a).

Runs the contract-adapter (contract-adapter/bin/adapter) on a legacy run
directory to emit data-contract artifacts (<run>/contract/{manifest,measurements}.json)
for the ingestor / dashboard.

The config's ``schema_version`` is the switch:
  * unset/null -> legacy runner -> run the adapter (this command)
  * set        -> a versioned runner emits contract natively -> adapter skipped

Also performs the cross-boundary version check: it compares the schema version
the adapter was built against (``adapter --schema-version``) with the
``bench-contract`` opam package available in the switch, and warns if the adapter
is out of date (rebuild via contract-adapter/build.sh).
"""
import logging
import os
import subprocess
from pathlib import Path

from running.config import Configuration

logger = logging.getLogger(__name__)


def setup_parser(subparsers):
    f = subparsers.add_parser("adapt")
    f.set_defaults(which="adapt")
    f.add_argument("RUN_DIR", type=Path,
                   help="a legacy run directory containing olly_/perf_ sidecars")
    f.add_argument("-o", "--out", type=Path, default=None,
                   help="output contract dir (default: RUN_DIR/contract)")
    f.add_argument("-c", "--config", type=Path, default=None,
                   help="the runbms config, to read its schema_version switch")
    f.add_argument("--adapter", type=Path, default=None,
                   help="path to the contract-adapter binary "
                        "(default: $RUNNING_CONTRACT_ADAPTER or contract-adapter/bin/adapter)")


def _find_adapter(explicit) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("RUNNING_CONTRACT_ADAPTER")
    if env:
        candidates.append(Path(env))
    # repo default: <repo>/contract-adapter/bin/adapter
    repo_root = Path(__file__).resolve().parents[3]
    candidates.append(repo_root / "contract-adapter" / "bin" / "adapter")
    for c in candidates:
        if c and c.exists() and os.access(c, os.X_OK):
            return c
    return None


def _installed_pkg_version() -> str:
    try:
        out = subprocess.run(["opam", "show", "bench-contract", "--field", "version"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            return out.stdout.strip().strip('"')
    except Exception:
        pass
    return None


def _major_minor(v: str):
    parts = (v or "").split(".")
    return tuple(parts[:2])


def _check_versions(adapter: Path) -> None:
    """Warn if the adapter's built-against schema is behind the installed package."""
    try:
        adapter_v = subprocess.run([str(adapter), "--schema-version"],
                                   capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        adapter_v = None
    pkg_v = _installed_pkg_version()
    if adapter_v and pkg_v:
        if _major_minor(adapter_v) != _major_minor(pkg_v):
            logger.warning(
                "contract-adapter was built against schema %s, but bench-contract %s is installed; "
                "the adapter is out of date -- rebuild it: (cd contract-adapter && ./build.sh)",
                adapter_v, pkg_v)
        else:
            logger.info("contract-adapter schema %s matches installed bench-contract %s",
                        adapter_v, pkg_v)
    elif adapter_v:
        logger.info("contract-adapter built against schema %s "
                    "(bench-contract package version not resolved in this switch)", adapter_v)


def run(args) -> bool:
    if args.get("which") != "adapt":
        return False

    run_dir = args["RUN_DIR"]

    # schema_version switch: a versioned runner emits contract natively.
    if args.get("config"):
        cfg_path = args["config"]
        cfg = Configuration.from_file(cfg_path.parent, cfg_path.name)
        schema_version = cfg.get("schema_version")
        if schema_version:
            logger.info("config declares schema_version=%s; a versioned runner emits contract "
                        "artifacts natively -- the legacy adapter is not needed", schema_version)
            return True

    adapter = _find_adapter(args.get("adapter"))
    if adapter is None:
        logger.error("contract-adapter binary not found; build it (cd contract-adapter && ./build.sh) "
                     "or pass --adapter / set RUNNING_CONTRACT_ADAPTER")
        return True

    _check_versions(adapter)

    out = args.get("out") or (run_dir / "contract")
    cmd = [str(adapter), str(run_dir), str(out)]

    # Resolve runbms.yml's runtime identity here, in Python, where PyYAML handles
    # anchors/merge keys that the OCaml YAML reader rejects. Hand the adapter a
    # clean JSON so it gets authoritative runtime identity (configure_args etc.)
    # rather than falling back to filename parsing.
    runbms = run_dir / "runbms.yml"
    rt_tmp = None
    if runbms.exists():
        try:
            import yaml, json, tempfile
            cfg = yaml.safe_load(runbms.read_text())
            runtimes = cfg.get("runtimes") if isinstance(cfg, dict) else None
            if runtimes:
                fd = tempfile.NamedTemporaryFile("w", suffix=".runtimes.json", delete=False)
                json.dump(runtimes, fd)
                fd.close()
                rt_tmp = fd.name
                cmd += ["--runtimes", rt_tmp]
        except Exception as e:
            logger.warning("could not pre-resolve %s (%s); adapter will use its own parse", runbms, e)

    logger.info("adapting %s -> %s (via %s)", run_dir, out, adapter)
    rc = subprocess.run(cmd).returncode
    if rt_tmp:
        try:
            os.remove(rt_tmp)
        except OSError:
            pass
    if rc != 0:
        logger.error("contract-adapter failed (rc=%d)", rc)
    return True
