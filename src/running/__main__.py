#!/usr/env/bin python3
import logging
import argparse

from running.__version__ import __VERSION__
from running.command import fillin, runbms, buildbms, minheap, log_preprocessor, adapt
from running.suite import set_dry_run
from running.runtime import OCaml, OpamRootBusyError
import sys
import importlib.resources
import os

logger = logging.getLogger(__name__)

MODULES = [fillin, runbms, buildbms, minheap, log_preprocessor, adapt]


def setup_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="change logging level to DEBUG")
    parser.add_argument("--version", action="version",
                        version="running {}".format(__VERSION__))
    parser.add_argument("-d", "--dry-run", action="store_true",
                        help="dry run")
    subparsers = parser.add_subparsers()
    for m in MODULES:
        m.setup_parser(subparsers)
    return parser


def main():
    parsers = setup_parser()
    args = vars(parsers.parse_args())

    # Config root logger
    if args.get("verbose") == True:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO
    logging.basicConfig(
        format="[%(levelname)s] %(asctime)s %(filename)s:%(lineno)d %(message)s",
        level=log_level)

    if args.get("dry_run") == True:
        set_dry_run(True)
    config_root = importlib.resources.files(__package__) / "config"
    try:
        with importlib.resources.as_file(config_root) as config_path:
            os.environ["RUNNING_NG_PACKAGE_DATA"] = str(config_path)
            for m in MODULES:
                if m.run(args):
                    break
            else:
                parsers.print_help()
    except OpamRootBusyError as e:
        # Expected, actionable condition — report it plainly rather than as a
        # traceback, and exit non-zero so scripts and CI notice.
        logger.error("%s", e)
        sys.exit(1)
    finally:
        # Provisioning a switch selects it, and removing a stale one deselects
        # whatever was active; put the user's original switch back either way,
        # including when the run failed or was interrupted.
        OCaml.restore_active_switch()
        OCaml.release_opam_lock()


if __name__ == "__main__":
    main()
