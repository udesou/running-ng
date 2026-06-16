"""Build-only command: compiles all benchmark binaries without running them."""
import logging
from typing import Dict, Set, Tuple
from running.suite import is_dry_run
from running.config import Configuration
from pathlib import Path
from running.util import parse_config_str
from running.runtime import Runtime
from running.command.runbms import expand_configs
import os

logger = logging.getLogger(__name__)


def setup_parser(subparsers):
    parser = subparsers.add_parser(
        "buildbms",
        help="Build all benchmark binaries without running them",
    )
    parser.set_defaults(which="buildbms")
    parser.add_argument("CONFIG", type=Path, help="Configuration file")


def run(args) -> bool:
    # Guard on the subcommand: every command carries a CONFIG, so without this
    # buildbms (listed before minheap in MODULES) would intercept them.
    if args.get("which") != "buildbms":
        return False
    if "CONFIG" not in args or args["CONFIG"] is None:
        return False

    config_path = args["CONFIG"]
    configuration = Configuration.from_file(Path(os.getcwd()), config_path)
    configuration.resolve_class()

    suites = configuration.get("suites")
    benchmarks = configuration.get("benchmarks")
    if benchmarks is None:
        benchmarks = {}
    configs = expand_configs(
        configuration.get("configs"),
        configuration.get("config_sweep"),
    )

    # Resolve unique runtimes from configs.
    runtime_by_config: Dict[str, Runtime] = {}
    for c in configs:
        runtime_by_config[c], _ = parse_config_str(configuration, c)

    # Deduplicate runtimes for reporting.
    unique_runtimes = {rt.name: rt for rt in runtime_by_config.values()}
    total_benchmarks = sum(len(bms) for bms in benchmarks.values())
    print(f"Building {total_benchmarks} benchmarks across "
          f"{len(unique_runtimes)} runtime(s): "
          f"{', '.join(unique_runtimes.keys())}")
    print()

    prepared: Set[Tuple[str, str, str]] = set()
    build_failed: Set[Tuple[str, str, str]] = set()
    build_count = 0

    for suite_name, bms in benchmarks.items():
        _ = suites[suite_name]
        for bm in bms:
            for c in configs:
                runtime = runtime_by_config[c]
                key = (suite_name, bm.name, runtime.name)
                if key in prepared or key in build_failed:
                    continue
                build_count += 1
                print(f"  [{build_count}] {suite_name}/{bm.name} [{runtime.name}] ... ",
                      end="", flush=True)
                try:
                    if not is_dry_run():
                        bm.prepare(runtime)
                    print("OK")
                except Exception as e:
                    print(f"FAILED")
                    logging.warning("  %s", e)
                    build_failed.add(key)
                    continue
                prepared.add(key)

    print()
    print(f"--- Build summary ---")
    print(f"  Succeeded: {len(prepared)}")
    print(f"  Failed:    {len(build_failed)}")

    if build_failed:
        print(f"\n--- Build failures ({len(build_failed)}) ---")
        for suite_name, bm_name, rt_name in sorted(build_failed):
            print(f"  FAIL {suite_name}/{bm_name} [{rt_name}]")

    if prepared:
        print(f"\n--- Successful builds ({len(prepared)}) ---")
        for suite_name, bm_name, rt_name in sorted(prepared):
            print(f"  OK   {suite_name}/{bm_name} [{rt_name}]")

    return True
