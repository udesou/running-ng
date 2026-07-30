# GENERATED from vocab.json by contract-adapter/gen_contract_py.py — DO NOT EDIT.
# Regenerate when the contract (bench-contract) changes.
import hashlib

SCHEMA_VERSION = "1.0"

# config_id recipe (must match the OCaml Registry.canonical_config_id exactly)
_CFG_PREFIX = "cfg_"
_CFG_FS = "\x1f"
_CFG_LS = ","

METRIC_CATALOG = {
    "wall_time": {
        "unit": "s",
        "layer": 1,
        "source": "olly"
    },
    "cpu_time": {
        "unit": "s",
        "layer": 1,
        "source": "olly"
    },
    "max_rss": {
        "unit": "KiB",
        "layer": 1,
        "source": "olly"
    },
    "mean_latency": {
        "unit": "ms",
        "layer": 1,
        "source": "olly"
    },
    "gc_overhead": {
        "unit": "pct",
        "layer": 2,
        "source": "olly"
    },
    "gc_time": {
        "unit": "s",
        "layer": 2,
        "source": "olly"
    },
    "minor_collections": {
        "unit": "count",
        "layer": 2,
        "source": "olly"
    },
    "major_collections": {
        "unit": "count",
        "layer": 2,
        "source": "olly"
    },
    "promoted_pct": {
        "unit": "pct",
        "layer": 2,
        "source": "olly"
    },
    "minor_words": {
        "unit": "words",
        "layer": 2,
        "source": "olly"
    },
    "major_words": {
        "unit": "words",
        "layer": 2,
        "source": "olly"
    },
    "instructions": {
        "unit": "count",
        "layer": 3,
        "source": "perf"
    },
    "cycles": {
        "unit": "count",
        "layer": 3,
        "source": "perf"
    },
    "page_faults": {
        "unit": "count",
        "layer": 3,
        "source": "perf"
    },
    "task_clock": {
        "unit": "ns",
        "layer": 3,
        "source": "perf"
    }
}
OLLY_FIELD_MAP = {
    "wall_time": "wall_time",
    "cpu_time": "cpu_time",
    "gc_time": "gc_time",
    "gc_overhead": "gc_overhead",
    "max_rss_kb": "max_rss",
    "mean_latency": "mean_latency",
    "allocations.promoted_pct": "promoted_pct",
    "allocations.minor_heap": "minor_words",
    "allocations.major_heap": "major_words",
    "collections.minor": "minor_collections",
    "collections.major": "major_collections"
}
PERF_EVENT_MAP = {
    "instructions": "instructions",
    "cycles": "cycles",
    "page-faults": "page_faults",
    "task-clock": "task_clock"
}
DIMENSION_OF_MODIFIER = {
    "s": {
        "dimension": "minor_heap",
        "unit": "words"
    },
    "o": {
        "dimension": "space_overhead",
        "unit": "pct"
    },
    "M": {
        "dimension": "custom_major_ratio",
        "unit": "pct"
    },
    "m": {
        "dimension": "custom_minor_ratio",
        "unit": "pct"
    },
    "re": {
        "dimension": "runtime_events_ring_log2",
        "unit": "log2_words"
    },
    "md": {
        "dimension": "max_domains",
        "unit": "count"
    },
    "re_par": {
        "dimension": "runtime_events_ring_log2",
        "unit": "log2_words"
    },
    "md_par": {
        "dimension": "max_domains",
        "unit": "count"
    },
    "plan": {
        "dimension": "gc_plan",
        "unit": "name"
    },
    "threads": {
        "dimension": "gc_threads",
        "unit": "count"
    }
}
OLLY_OUTPUT_VERSION_SUPPORTED = [
    1,
    2
]
TOOL_SUPPORTED_VERSIONS = {
    "olly": [
        "0.5"
    ],
    "perf": [
        "6"
    ]
}


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
