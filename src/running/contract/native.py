"""Native contract emission for running-ng (used when a config sets schema_version).

Builds data-contract measurement + manifest artifacts *during* a run, in pure
Python, from the vocabulary generated out of the OCaml contract. Unlike the
adapter, identity comes from running-ng's in-memory knowledge (the runtime spec
in the merged config, the applied config string, the invocation counter) rather
than from filenames — the raw olly/perf sidecars stay as archival, referenced by
nothing here (they remain in the log dir). OCaml only verifies the result.
"""
import datetime
import json
import os
import re
import socket
import subprocess
from pathlib import Path

from running.contract import emit, vocab


def _machine():
    m = {"hostname": socket.gethostname()}
    try:
        m["kernel"] = os.uname().release
    except Exception:
        pass
    c = os.cpu_count()
    if c:
        m["cores"] = c
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                m["cpu_model"] = line.split(":", 1)[1].strip()
                break
    except Exception:
        pass
    return m


def _tool_version(cmd):
    # extract a version number (e.g. "6.17.13") from `<tool> --version` output;
    # returns None if the tool has no --version (e.g. olly prints usage instead).
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        text = (out.stdout or "") + "\n" + (out.stderr or "")
        m = re.search(r"\d+\.\d+(?:\.\d+)?", text)
        return m.group(0) if m else None
    except Exception:
        return None


def _iso_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class NativeEmitter:
    """Accumulates contract artifacts across a run; finalize() writes the manifest."""

    def __init__(self, contract_dir, run_id, runtimes):
        # `runtimes`: the raw runtimes dict {name -> {type, version, commit,
        # configure_args}} captured BEFORE Configuration.resolve_class() turns the
        # values into Runtime objects.
        self.dir = Path(contract_dir)
        self.run_id = run_id
        self.runtimes = runtimes or {}
        self.configs = {}         # config_id -> descriptor
        self.benchmarks = {}      # (name, suite) -> ref
        self._inv = {}            # (bench, config_id) -> next invocation index
        self._cfg_cache = {}      # config_str -> descriptor
        (self.dir / "measurements").mkdir(parents=True, exist_ok=True)
        # fresh files (avoid appending to stale output from a prior run in the same dir)
        for tool in ("olly", "perf"):
            p = self.dir / "measurements" / (tool + ".ndjson")
            if p.exists():
                p.unlink()

    def _descriptor(self, config_str):
        d = self._cfg_cache.get(config_str)
        if d is not None:
            return d
        runtime_name = config_str.split("|")[0]
        spec = self.runtimes.get(runtime_name, {}) or {}
        kind = spec.get("type", "OCaml")
        version = spec.get("version") or runtime_name
        commit = spec.get("commit") or spec.get("hash")
        options = spec.get("configure_args") or []
        dims = {}
        for tok in config_str.split("|")[1:]:
            parts = tok.split("-")
            name = parts[0]
            if len(parts) >= 2 and name in vocab.DIMENSION_OF_MODIFIER:
                v = parts[1]
                try:
                    v = int(v)
                except ValueError:
                    pass
                dims.setdefault(vocab.DIMENSION_OF_MODIFIER[name]["dimension"], v)
        d = emit.config_descriptor(kind, version, commit, options, dims,
                                   runtime_name=runtime_name,
                                   modifiers=config_str.split("|")[1:],
                                   tools=["olly", "perf"])
        self._cfg_cache[config_str] = d
        self.configs[d["config_id"]] = d
        return d

    def record(self, bm, config_str, companion_out):
        """One invocation: split the {olly, perf} companion into per-tool NDJSON."""
        if not companion_out:
            return
        try:
            data = json.loads(companion_out)
        except Exception:
            return
        cfg = self._descriptor(config_str)
        cid = cfg["config_id"]
        name, suite = bm.name, bm.suite_name
        self.benchmarks[(name, suite)] = {"name": name, "suite": suite}
        key = (name, cid)
        inv = self._inv.get(key, 0)
        self._inv[key] = inv + 1
        for tool, metrics in (("olly", emit.olly_metrics(data.get("olly"))),
                              ("perf", emit.perf_metrics(data.get("perf")))):
            if data.get(tool) is not None:
                m = emit.measurement(self.run_id, name, suite, cid, inv, metrics)
                emit.append_ndjson(str(self.dir / "measurements" / (tool + ".ndjson")), m)

    def finalize(self):
        tv = {}
        for tool, cmd in (("olly", ["olly", "--version"]), ("perf", ["perf", "--version"])):
            v = _tool_version(cmd)
            if v:
                tv[tool] = v
        man = emit.manifest(self.run_id, _iso_now(), _machine(),
                            list(self.configs.values()),
                            tool_versions=tv, comparisons=None,
                            benchmarks=list(self.benchmarks.values()),
                            produced_by="running-ng (native)")
        emit.write_json(str(self.dir / "manifest.json"), man)
        return len(self.configs), len(self.benchmarks)
