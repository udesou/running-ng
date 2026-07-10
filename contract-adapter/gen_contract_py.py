#!/usr/bin/env python3
"""Generate a Python contract-vocabulary module from the OCaml-exported vocab.json.

This is the running-ng-side generator (item: "generation sits within running-ng").
It reads the contract's vocab.json (produced by `dune exec tools/gen_vocab` in the
bench-contract repo) and emits src/running/contract/vocab.py: the canonical metric
catalog, raw->canonical field maps, modifier->dimension map, supported tool
versions, and a config_id() that reproduces the OCaml algorithm bit-for-bit.

When the contract changes, regenerate: bump bench-contract, re-run gen_vocab, then
run this generator. Structural changes to vocab.json => update this generator.

Usage: gen_contract_py.py <vocab.json> [out.py]
"""
import json
import sys
from pathlib import Path

# Token-replacement template (NOT str.format — the emitted code contains literal
# braces like `{}` that would confuse .format).
TEMPLATE = '''\
# GENERATED from vocab.json by contract-adapter/gen_contract_py.py — DO NOT EDIT.
# Regenerate when the contract (bench-contract) changes.
import hashlib

SCHEMA_VERSION = "@@SCHEMA_VERSION@@"

# config_id recipe (must match the OCaml Registry.canonical_config_id exactly)
_CFG_PREFIX = "@@CFG_PREFIX@@"
_CFG_FS = "@@CFG_FS@@"
_CFG_LS = "@@CFG_LS@@"

METRIC_CATALOG = @@METRIC_CATALOG@@
OLLY_FIELD_MAP = @@OLLY_FIELD_MAP@@
PERF_EVENT_MAP = @@PERF_EVENT_MAP@@
DIMENSION_OF_MODIFIER = @@DIMENSION_OF_MODIFIER@@
OLLY_OUTPUT_VERSION_SUPPORTED = @@OLLY_OUTPUT_VERSION_SUPPORTED@@
TOOL_SUPPORTED_VERSIONS = @@TOOL_SUPPORTED_VERSIONS@@


def _dimval(v):
    # matches OCaml dim_value_to_string; bool is checked before int on purpose
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return "%g" % v
    return str(v)


def config_id(kind, version, commit, options, dimensions):
    """Content hash of a config's normative fields; identical to the OCaml side."""
    opts = _CFG_LS.join(sorted(options or []))
    dims = _CFG_LS.join(sorted("%s=%s" % (k, _dimval(v)) for k, v in (dimensions or {}).items()))
    canonical = _CFG_FS.join([kind, version, commit or "", opts, dims])
    return _CFG_PREFIX + hashlib.md5(canonical.encode("utf-8")).hexdigest()
'''


def pyliteral(obj):
    # json is valid Python for our dicts (str keys; str/int/list values)
    return json.dumps(obj, indent=4, ensure_ascii=False)


def main():
    if len(sys.argv) < 2:
        print("usage: gen_contract_py.py <vocab.json> [out.py]", file=sys.stderr)
        sys.exit(2)
    vocab = json.load(open(sys.argv[1]))
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else \
        Path(__file__).resolve().parents[1] / "src" / "running" / "contract" / "vocab.py"
    cfg = vocab["config_id"]
    subs = {
        "@@SCHEMA_VERSION@@": vocab["schema_version"],
        "@@CFG_PREFIX@@": cfg["prefix"],
        # escape control char (unit separator) into a Python-source-safe form
        "@@CFG_FS@@": cfg["field_separator"].encode("unicode_escape").decode("ascii"),
        "@@CFG_LS@@": cfg["list_separator"],
        "@@METRIC_CATALOG@@": pyliteral(vocab["metric_catalog"]),
        "@@OLLY_FIELD_MAP@@": pyliteral(vocab["olly_field_map"]),
        "@@PERF_EVENT_MAP@@": pyliteral(vocab["perf_event_map"]),
        "@@DIMENSION_OF_MODIFIER@@": pyliteral(vocab["dimension_of_modifier"]),
        "@@OLLY_OUTPUT_VERSION_SUPPORTED@@": pyliteral(vocab["olly_output_version_supported"]),
        "@@TOOL_SUPPORTED_VERSIONS@@": pyliteral(vocab["tool_supported_versions"]),
    }
    text = TEMPLATE
    for k, v in subs.items():
        text = text.replace(k, v)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print("wrote", out)


if __name__ == "__main__":
    main()
