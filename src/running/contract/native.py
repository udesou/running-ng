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
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict

from running import osinfo
from running.contract import emit, vocab


def _machine():
    # Annotated: the values are a mix of str and int, and an unannotated literal
    # would be inferred as dict[str, str].
    m = {"hostname": socket.gethostname()}  # type: Dict[str, Any]
    try:
        m["kernel"] = os.uname().release
    except Exception:
        pass
    c = osinfo.core_count()
    if c:
        m["cores"] = c
    # Via osinfo so the manifest carries a CPU model on macOS and FreeBSD too;
    # the /proc/cpuinfo read this used to do inline left the field absent there.
    model = osinfo.cpu_model()
    if model:
        m["cpu_model"] = model
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


def _olly_version():
    # Version of the olly that running-ng ACTUALLY runs. olly is invoked as `olly`
    # on PATH, so shutil.which("olly") from inside this process resolves the exact
    # binary — no OLLY_DIR/env reliance, so a per-runtime/per-switch olly build is
    # reflected. olly has no --version flag, so derive the version from whatever
    # owns that binary: the opam switch it lives in, or the git checkout it was
    # built from.
    w = shutil.which("olly")
    if not w:
        return None
    real = Path(os.path.realpath(w))
    parts = real.parts
    # (a) opam switch that owns this binary: .../.opam/<switch>/bin/olly
    if ".opam" in parts:
        i = parts.index(".opam")
        if i + 1 < len(parts):
            switch = parts[i + 1]
            try:
                out = subprocess.run(
                    ["opam", "show", "runtime_events_tools", "--field", "version", "--switch", switch],
                    capture_output=True, text=True, timeout=15)
                v = out.stdout.strip().strip('"')
                if v:
                    return v
            except Exception:
                pass
    # (b) git checkout the binary was built from (e.g. an OLLY_DIR/_build install)
    for anc in real.parents:
        if (anc / ".git").exists():
            try:
                out = subprocess.run(["git", "-C", str(anc), "describe", "--tags", "--always"],
                                     capture_output=True, text=True, timeout=10)
                v = out.stdout.strip()
                if v:
                    return v
            except Exception:
                pass
            break
    return None


class NativeEmitter:
    """Accumulates contract artifacts across a run; finalize() writes the manifest."""

    def __init__(self, contract_dir, run_id, runtimes, comparisons=None):
        # `runtimes`: the raw runtimes dict {name -> {type, version, commit,
        # configure_args}} captured BEFORE Configuration.resolve_class() turns the
        # values into Runtime objects. `comparisons`: the config's comparisons block.
        self.dir = Path(contract_dir)
        self.run_id = run_id
        self.runtimes = runtimes or {}
        self.comparisons = comparisons or []
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
            entry = vocab.DIMENSION_OF_MODIFIER.get(name)
            if not entry:
                continue
            if "value" in entry:            # flag modifier, e.g. mmtk_bactrian
                v = entry["value"]
            elif len(parts) >= 2:           # name-value modifier, e.g. s-32768
                v = parts[1]
                try:
                    v = int(v)
                except ValueError:
                    pass
            else:
                continue
            dims.setdefault(entry["dimension"], v)
        d = emit.config_descriptor(kind, version, commit, options, dims,
                                   runtime_name=runtime_name,
                                   modifiers=config_str.split("|")[1:],
                                   tools=["olly", "perf"])
        self._cfg_cache[config_str] = d
        self.configs[d["config_id"]] = d
        return d

    def record(self, bm, config_str, companion_out, ok=True):
        """One invocation: split the {olly, perf} companion into per-tool NDJSON.

        `ok` is the runner's verdict (exit status == Normal). A crashed/timed-out
        invocation is dropped wholesale — its partial olly/perf output would
        otherwise show up as crash-time garbage in the dashboard."""
        if not companion_out or not ok:
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
        olly_m = emit.olly_metrics(data.get("olly"))
        # Drop crashed invocations wholesale (both tools): their olly wall_time is
        # non-positive and their perf counters are a partial-run count — emitting
        # either pollutes the dashboard with crash-time garbage.
        if emit.crashed(olly_m):
            return
        for tool, metrics in (("olly", olly_m),
                              ("perf", emit.perf_metrics(data.get("perf")))):
            if data.get(tool) is not None:
                m = emit.measurement(self.run_id, name, suite, cid, inv, metrics)
                emit.append_ndjson(str(self.dir / "measurements" / (tool + ".ndjson")), m)

    def _runtime_selector(self, name):
        """A normative selector isolating runtime `name` (by version/options/commit
        — never the advisory _runtime_name). `runtime.options` is pinned ALWAYS,
        even when empty: a stock build has options=[] and must NOT be matched by
        its own same-version variants (fp / flambda), whose selectors carry
        non-empty options. Omitting the empty list under-specifies the selector."""
        spec = self.runtimes.get(name, {}) or {}
        sel = {"runtime.version": spec.get("version") or name}
        sel["runtime.options"] = spec.get("configure_args") or []
        commit = spec.get("commit") or spec.get("hash")
        if commit:
            sel["runtime.commit"] = commit
        return sel

    def _map_comparisons(self):
        """running-ng a/b comparison blocks (inter-runtime) -> contract comparisons.
        A list on `a` (n>1) splits into one comparison per baseline (§4.5)."""
        out = []
        for block in self.comparisons:
            if not isinstance(block, dict) or block.get("a") is None or block.get("b") is None:
                continue
            a = block["a"]; b = block["b"]
            a_list = a if isinstance(a, list) else [a]
            b_list = b if isinstance(b, list) else [b]
            for base in a_list:
                c = {
                    "kind": "inter",
                    "over": "runtime",
                    "mode": block.get("mode", "pairwise"),
                    "baseline": self._runtime_selector(base),
                    "variants": [self._runtime_selector(x) for x in b_list],
                }
                if block.get("label"):
                    c["label"] = block["label"]
                out.append(c)
        return out

    def finalize(self):
        tv = {}
        pv = _tool_version(["perf", "--version"])
        if pv:
            tv["perf"] = pv
        ov = _olly_version()
        if ov:
            tv["olly"] = ov
        man = emit.manifest(self.run_id, _iso_now(), _machine(),
                            list(self.configs.values()),
                            tool_versions=tv, comparisons=self._map_comparisons(),
                            benchmarks=list(self.benchmarks.values()),
                            produced_by="running-ng (native)")
        emit.write_json(str(self.dir / "manifest.json"), man)
        return len(self.configs), len(self.benchmarks)
