#!/usr/bin/env python3
"""Print a hotspots-style summary from a run's memtrace JSON sidecars.

Reads every memtrace_*.json(.gz) in the given run directory (produced by
runbms.py's write_memtrace_json_sidecar — a folded-stack summary of each
invocation's raw .trace, generated via the runtime's own
memtrace_flamegraph tool) and prints the top allocation sites by sample
count, aggregated by leaf frame across all invocations found.

Usage: summarize_json.py <run_dir>
"""
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path


def load_records(path: Path):
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt") as f:
        return json.load(f)


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <run_dir>", file=sys.stderr)
        sys.exit(1)
    run_dir = Path(sys.argv[1])
    json_files = sorted(run_dir.glob("memtrace_*.json")) + sorted(run_dir.glob("memtrace_*.json.gz"))
    if not json_files:
        print(f"No memtrace_*.json(.gz) sidecars found in {run_dir}", file=sys.stderr)
        sys.exit(1)

    by_leaf = defaultdict(int)
    total_samples = 0
    for jf in json_files:
        records = load_records(jf)
        for r in records:
            by_leaf[r["stack"][-1]] += r["samples"]
            total_samples += r["samples"]
        print(f"{jf.name}: {len(records)} distinct stacks")

    print(f"\n{total_samples} total samples across {len(json_files)} invocation(s)\n")
    print("Top allocation sites (by leaf frame):")
    for leaf, samples in sorted(by_leaf.items(), key=lambda kv: -kv[1])[:15]:
        pct = 100.0 * samples / total_samples if total_samples else 0.0
        print(f"  {samples:6d} ({pct:4.1f}%)  {leaf}")


if __name__ == "__main__":
    main()
