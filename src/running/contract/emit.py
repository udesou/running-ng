"""Emit data-contract artifacts natively, using the generated contract.vocab
so config_ids and metric names match the OCaml adapter's.
"""
import json
import logging
import os

from running.contract import vocab


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


_warned_olly_versions = set()


def _check_olly_output_version(olly):
    """Warn once per olly output version not in OLLY_OUTPUT_VERSION_SUPPORTED.

    Warn rather than raise: an unknown version usually still carries the fields we read.
    """
    v = olly.get("version")
    if v is None or v in vocab.OLLY_OUTPUT_VERSION_SUPPORTED:
        return
    if v not in _warned_olly_versions:
        _warned_olly_versions.add(v)
        logging.warning(
            "olly output version %s not in supported set %s — metrics may be "
            "misparsed (regenerate vocab.py from the contract's registry.ml)",
            v, vocab.OLLY_OUTPUT_VERSION_SUPPORTED)


def olly_metrics(olly):
    out = []
    if not isinstance(olly, dict):
        return out
    _check_olly_output_version(olly)
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
    """True if olly's wall_time/cpu_time are non-positive, which happens when the
    process died before emitting runtime events. The perf counters of such an
    invocation are partial too, so callers drop the whole invocation."""
    for m in olly_metrics_list or []:
        if m["name"] in ("wall_time", "cpu_time") and m["value"] <= 0:
            return True
    return False


def dimensions_from_modifiers(modifiers):
    """Map applied modifiers ({name: value}, excludes already honoured) to canonical dimensions."""
    dims = {}
    for name, value in modifiers.items():
        d = vocab.DIMENSION_OF_MODIFIER.get(name)
        if d:
            # flag modifiers carry a fixed value in the table
            dims.setdefault(d["dimension"], d.get("value", value))
    return dims


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
