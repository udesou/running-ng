"""Emit data-contract artifacts from running-ng, in pure Python.

Uses the generated vocab (contract.vocab) as the single source of the canonical
names, mappings, and config_id algorithm, so running-ng's native output is
verifiably conformant and its config_ids join with anything the OCaml side
produces. running-ng stays Python; OCaml only *verifies* the result.

Normalization here mirrors the OCaml adapter (metric maps, dimension map,
config_id); the difference is that identity comes from running-ng's in-memory
knowledge instead of being parsed from filenames.
"""
import json
import os

from running.contract import vocab


# --- metric normalization (raw olly/perf -> canonical metrics) -----------------

def _dotted(obj, path):
    cur = obj
    for k in path.split("."):
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur


def _metric(name, value):
    d = vocab.METRIC_CATALOG.get(name)
    if d is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return {"name": name, "value": v, "unit": d["unit"], "source": d["source"], "layer": d["layer"]}


def olly_metrics(olly):
    out = []
    if not isinstance(olly, dict):
        return out
    for path, name in vocab.OLLY_FIELD_MAP.items():
        v = _dotted(olly, path)
        if v is not None:
            m = _metric(name, v)
            if m:
                out.append(m)
    return out


def perf_metrics(perf):
    out = []
    if not isinstance(perf, list):
        return out
    for e in perf:
        name = vocab.PERF_EVENT_MAP.get(e.get("event"))
        if name:
            m = _metric(name, e.get("counter-value"))
            if m:
                out.append(m)
    return out


def crashed(olly_metrics_list):
    """True if an invocation's olly metrics show the process aborted rather than
    completing. olly derives wall_time/cpu_time from the first/last runtime-events
    timestamps, so a process that dies before emitting proper events yields a
    non-positive (in practice hugely negative) wall_time. Its perf counters are
    then a partial-run count too, so the WHOLE invocation must be dropped — not
    just the olly side — or the dashboard shows crash-time garbage (e.g. an LXR
    bench "finishing" in a fraction of stock's instructions)."""
    for m in olly_metrics_list or []:
        if m["name"] in ("wall_time", "cpu_time") and m["value"] <= 0:
            return True
    return False


def dimensions_from_modifiers(modifiers):
    """modifiers: {name: value} that running-ng actually applied (honoring excludes).

    Maps to canonical dimensions via the registry. Skips the lavyek-scoped _par
    duplicates (same axis as re/md) — running-ng, unlike the adapter, knows which
    actually applied, so it should simply not pass the excluded ones."""
    dims = {}
    for name, value in modifiers.items():
        d = vocab.DIMENSION_OF_MODIFIER.get(name)
        if d:
            # flag modifiers (mmtk_bactrian, …) carry a fixed value in the table
            dims.setdefault(d["dimension"], d.get("value", value))
    return dims


# --- record builders (contract shapes) -----------------------------------------

def config_descriptor(kind, version, commit=None, options=None, dimensions=None,
                      runtime_name=None, modifiers=None, tools=None):
    options = options or []
    dimensions = dimensions or {}
    cid = vocab.config_id(kind, version, commit, options, dimensions)
    rt = {"kind": kind, "version": version}
    if commit:
        rt["commit"] = commit
    if options:
        rt["options"] = options
    c = {"config_id": cid, "runtime": rt}
    if dimensions:
        c["dimensions"] = dimensions
    if tools:
        c["tools"] = tools
    if runtime_name:
        c["_runtime_name"] = runtime_name
    if modifiers:
        c["_modifiers"] = modifiers
    return c


def measurement(run_id, benchmark, suite, config_id_, invocation, metrics,
                raw_ref=None, tags=None):
    bench = {"name": benchmark, "suite": suite}
    if tags:
        bench["tags"] = tags
    m = {
        "schema_version": vocab.SCHEMA_VERSION,
        "run_id": run_id,
        "benchmark": bench,
        "config": {"config_id": config_id_},
        "invocation": invocation,
        "metrics": metrics,
    }
    if raw_ref:
        m["raw_ref"] = raw_ref
    return m


def manifest(run_id, created_at, machine, configs, tool_versions=None,
             comparisons=None, benchmarks=None, produced_by=None):
    m = {
        "schema_version": vocab.SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": created_at,
        "machine": machine,
        "configs": configs,
    }
    if tool_versions:
        m["tool_versions"] = tool_versions
    if comparisons:
        m["comparisons"] = comparisons
    if benchmarks:
        m["benchmarks"] = benchmarks
    if produced_by:
        m["_produced_by"] = produced_by
    return m


# --- writers -------------------------------------------------------------------

def append_ndjson(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj, separators=(",", ":")))
        f.write("\n")


def write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
