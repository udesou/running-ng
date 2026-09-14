#!/usr/bin/env python3
"""Merge a partial rerun into a completed run's artifacts.

Written for merging the 2026-08-28 micro fp/flambda rerun (35 fixed
benchmarks) into the main run, but generic over any pair of run dirs that
share config strings (and therefore contract config_ids).

    python3 scripts/merge_micro_runs.py MAIN_RUN_DIR RERUN_DIR MERGED_DIR

- MERGED_DIR is created as a full copy of MAIN_RUN_DIR, then every benchmark
  present in RERUN_DIR has its artifacts REPLACED by the rerun's: legacy
  .log / olly_* / perf_* files, and its rows in contract/measurements/*.ndjson.
  Replacement (not append) is deliberate: a benchmark was rerun because its
  data in the main run was crashed, mismeasured, or absent.
- contract/manifest.json: config_ids must match between the two runs (that is
  what makes the merge sound); benchmarks are unioned; a _merged_from note
  records provenance. Measurement rows keep their own run_id, so a reader can
  still tell which run produced which row.
"""
import json
import shutil
import sys
from pathlib import Path


def bench_names(run_dir: Path):
    """Benchmark names present in a run dir, from its log filenames."""
    names = set()
    for f in run_dir.glob("*.log"):
        stem = f.name.split(".")[0]
        if stem not in ("runbms",):
            names.add(stem)
    return names


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main_dir, rerun_dir, merged_dir = (Path(p) for p in sys.argv[1:4])
    for d in (main_dir, rerun_dir):
        if not (d / "contract" / "manifest.json").exists():
            sys.exit(f"{d}: no contract/manifest.json (pass the timestamped "
                     "run subdir, not the LOG_DIR)")
    if merged_dir.exists():
        sys.exit(f"{merged_dir} already exists; refusing to overwrite")

    man_a = json.loads((main_dir / "contract" / "manifest.json").read_text())
    man_b = json.loads((rerun_dir / "contract" / "manifest.json").read_text())
    ids_a = {c["config_id"] for c in man_a["configs"]}
    ids_b = {c["config_id"] for c in man_b["configs"]}
    if ids_a != ids_b:
        sys.exit("config_id sets differ between the runs — the configs are "
                 f"not the same, refusing to merge.\n  main:  {sorted(ids_a)}"
                 f"\n  rerun: {sorted(ids_b)}")

    replaced = bench_names(rerun_dir)
    print(f"replacing {len(replaced)} benchmarks from the rerun:")
    for n in sorted(replaced):
        print(f"  {n}")

    shutil.copytree(main_dir, merged_dir)

    # Legacy artifacts: drop the main run's files for replaced benchmarks,
    # copy the rerun's in. Filenames encode benchmark+config, so a rerun file
    # would otherwise silently shadow nothing / coexist ambiguously.
    dropped = 0
    for f in list(merged_dir.iterdir()):
        stem = f.name.split(".")[0]
        for prefix in ("olly_", "perf_", "memtrace_"):
            if stem.startswith(prefix):
                stem = stem[len(prefix):]
                break
        if stem in replaced and f.suffix in (".log", ".json", ".trace", ".gz"):
            f.unlink()
            dropped += 1
    copied = 0
    for f in rerun_dir.iterdir():
        if f.name in ("runbms.yml", "runbms_args.yml") or f.is_dir():
            continue
        shutil.copy2(f, merged_dir / f.name)
        copied += 1
    print(f"legacy artifacts: dropped {dropped} stale files, copied {copied}")

    # Contract measurements: drop replaced benchmarks' rows, append rerun rows.
    for tool in ("olly", "perf"):
        out = []
        kept = dropped_rows = added = 0
        pa = merged_dir / "contract" / "measurements" / f"{tool}.ndjson"
        for line in pa.read_text().splitlines():
            if not line.strip():
                continue
            if json.loads(line)["benchmark"]["name"] in replaced:
                dropped_rows += 1
            else:
                out.append(line)
                kept += 1
        pb = rerun_dir / "contract" / "measurements" / f"{tool}.ndjson"
        for line in pb.read_text().splitlines():
            if line.strip():
                out.append(line)
                added += 1
        pa.write_text("\n".join(out) + "\n")
        print(f"{tool}.ndjson: kept {kept}, dropped {dropped_rows}, added {added}")

    # Manifest: union benchmarks, record provenance.
    seen = {(b["name"], b.get("suite")) for b in man_a["benchmarks"]}
    merged_benchmarks = list(man_a["benchmarks"])
    for b in man_b["benchmarks"]:
        if (b["name"], b.get("suite")) not in seen:
            merged_benchmarks.append(b)
    man_a["benchmarks"] = merged_benchmarks
    man_a["_merged_from"] = [man_a.get("run_id"), man_b.get("run_id")]
    man_a["_replaced_benchmarks"] = sorted(replaced)
    (merged_dir / "contract" / "manifest.json").write_text(
        json.dumps(man_a, indent=2) + "\n")
    print(f"manifest: {len(merged_benchmarks)} benchmarks, "
          f"merged from {man_a['_merged_from']}")

    # Sanity: every benchmark should now have rows for every config in perf.
    import collections
    cnt = collections.Counter()
    p = merged_dir / "contract" / "measurements" / "perf.ndjson"
    for line in p.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            cnt[(r["benchmark"]["name"], r["config"]["config_id"])] += 1
    per_bench = collections.defaultdict(set)
    for (b, c), n in cnt.items():
        per_bench[b].add(c)
    incomplete = {b: len(cs) for b, cs in per_bench.items() if len(cs) != len(ids_a)}
    if incomplete:
        print(f"WARNING: {len(incomplete)} benchmarks lack some configs: {incomplete}")
    else:
        print(f"sanity: all {len(per_bench)} benchmarks have all "
              f"{len(ids_a)} configs in perf.ndjson")


if __name__ == "__main__":
    main()
