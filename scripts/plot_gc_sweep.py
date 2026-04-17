#!/usr/bin/env python3
import argparse
import csv
import gzip
import json
import re
from pathlib import Path

from typing import Optional

import numpy as np
import pandas as pd


LOG_GLOB = "binarytrees*.log"


def _edges(vals: np.ndarray, log: bool) -> np.ndarray:
    vals = np.asarray(vals, dtype=float)
    if len(vals) == 1:
        v = vals[0]
        return np.array([v / 2.0, v * 2.0]) if log else np.array([v - 0.5, v + 0.5])

    if log:
        lv = np.log10(vals)
        mids = (lv[:-1] + lv[1:]) / 2.0
        e = np.empty(len(vals) + 1, dtype=float)
        e[1:-1] = 10 ** mids
        e[0] = 10 ** (lv[0] - (mids[0] - lv[0]))
        e[-1] = 10 ** (lv[-1] + (lv[-1] - mids[-1]))
        return e

    mids = (vals[:-1] + vals[1:]) / 2.0
    e = np.empty(len(vals) + 1, dtype=float)
    e[1:-1] = mids
    e[0] = vals[0] - (mids[0] - vals[0])
    e[-1] = vals[-1] + (vals[-1] - mids[-1])
    return e


def _read_metric_int(text: str, metric: str) -> int:
    m = re.search(rf"^{re.escape(metric)}:\s*(\d+)", text, re.M)
    if not m:
        raise ValueError(f"Missing metric: {metric}")
    return int(m.group(1))


def _read_elapsed(text: str) -> float:
    m = re.search(r"([0-9]+\.[0-9]+)elapsed", text)
    if not m:
        raise ValueError("Missing elapsed")
    return float(m.group(1))


def _read_maxresident_kb(text: str) -> int:
    m = re.search(r"([0-9]+)maxresident\)k", text)
    if not m:
        raise ValueError("Missing maxresident")
    return int(m.group(1))


def _read_olly_time_s(text: str, label: str) -> float:
    m = re.search(rf"^{re.escape(label)}:\s*([0-9]+(?:\.[0-9]+)?)\s*$", text, re.M)
    if not m:
        raise ValueError(f"Missing olly metric: {label}")
    return float(m.group(1))


def _find_json_sidecar(log_path: Path) -> Optional[Path]:
    """Find the .json sidecar for a .log file (plain or gzipped)."""
    stem = log_path.name
    # Strip .log or .log.gz suffix to get the base name
    if stem.endswith(".log.gz"):
        base = stem[: -len(".log.gz")]
    elif stem.endswith(".log"):
        base = stem[: -len(".log")]
    else:
        return None
    for candidate in [
        log_path.parent / f"{base}.json",
        log_path.parent / f"{base}.json.gz",
    ]:
        if candidate.exists():
            return candidate
    return None


def _read_json_sidecar(path: Path) -> Optional[dict]:
    """Read and parse a JSON sidecar file (NDJSON, plain or gzipped).

    A sidecar contains one compact JSON object per line (one per invocation).
    We return the last one — matching the .log convention of appending.
    """
    try:
        if path.name.endswith(".gz"):
            raw = gzip.open(path, "rb").read()
        else:
            raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        # NDJSON: take the last non-empty line
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line:
                return json.loads(line)
        return None
    except Exception:
        return None


def _parse_from_json(data: dict, log_name: str, s: int, o: int) -> dict:
    """Build a row dict from structured JSON sidecar data."""
    row: dict = {
        "log_file": log_name,
        "s": s,
        "o": o,
    }

    # olly gc-stats --json metrics (flat top-level fields)
    olly = data.get("olly", {})
    row["olly_wall_time_s"] = olly.get("wall_time", np.nan)
    row["olly_cpu_time_s"] = olly.get("cpu_time", np.nan)
    row["olly_gc_time_s"] = olly.get("gc_time", np.nan)
    row["olly_gc_overhead_pct"] = olly.get("gc_overhead", np.nan)
    row["max_rss_kb"] = olly.get("max_rss_kb", np.nan)
    row["max_rss_mb"] = row["max_rss_kb"] / 1024.0 if row["max_rss_kb"] and not (isinstance(row["max_rss_kb"], float) and np.isnan(row["max_rss_kb"])) else np.nan

    # Allocation stats from olly (replaces OCAMLRUNPARAM gc_verbose)
    alloc = olly.get("allocations", {})
    row["minor_words"] = alloc.get("minor_heap", np.nan)
    row["promoted_words"] = alloc.get("promoted_words", np.nan)
    row["major_words"] = alloc.get("major_heap", np.nan)

    # Collection counts from olly
    collections = olly.get("collections", {})
    row["minor_collections"] = collections.get("minor", np.nan)
    row["major_collections"] = collections.get("major", np.nan)
    row["forced_major_collections"] = collections.get("forced_major", np.nan)
    row["compactions"] = collections.get("compactions", np.nan)

    row["promotion_rate"] = row["promoted_words"] / row["minor_words"] if row["minor_words"] else np.nan
    row["major_per_minor"] = row["major_collections"] / row["minor_collections"] if row["minor_collections"] else np.nan

    # perf stat --json output: array of {"event": "cycles", "counter-value": "123", ...}
    perf_list = data.get("perf", [])
    for entry in perf_list:
        event = entry.get("event")
        if not event:
            continue
        try:
            row[f"perf_{event}"] = float(entry["counter-value"])
        except (KeyError, ValueError, TypeError):
            row[f"perf_{event}"] = np.nan
        # Also store the derived metric if present (e.g. "insn per cycle", "GHz")
        metric_val = entry.get("metric-value")
        metric_unit = entry.get("metric-unit")
        if metric_val is not None and metric_unit:
            safe_unit = re.sub(r"[^a-zA-Z0-9_]+", "_", metric_unit).strip("_").lower()
            try:
                row[f"perf_{event}_{safe_unit}"] = float(metric_val)
            except (ValueError, TypeError):
                pass

    return row


def parse_log(path: Path) -> dict:
    m = re.search(r"\.s-(\d+)\.o-(\d+)\.", path.name)
    if not m:
        raise ValueError(f"Cannot parse s/o from filename: {path.name}")
    s, o = map(int, m.groups())

    # Try structured JSON sidecar first
    json_path = _find_json_sidecar(path)
    if json_path is not None:
        data = _read_json_sidecar(json_path)
        if data is not None:
            return _parse_from_json(data, path.name, s, o)

    # Fallback: regex-parse the text log (backward compat for old runs)
    text = path.read_text()

    row = {
        "log_file": path.name,
        "s": s,
        "o": o,
    }

    # /usr/bin/time metrics (optional).
    try:
        row["elapsed_s"] = _read_elapsed(text)
    except ValueError:
        row["elapsed_s"] = np.nan
    try:
        row["maxresident_kb"] = _read_maxresident_kb(text)
        row["maxresident_mb"] = row["maxresident_kb"] / 1024.0
    except ValueError:
        row["maxresident_kb"] = np.nan
        row["maxresident_mb"] = np.nan

    # olly gc-stats metrics (optional).
    try:
        row["olly_wall_time_s"] = _read_olly_time_s(text, "Wall time (s)")
    except ValueError:
        row["olly_wall_time_s"] = np.nan
    try:
        row["olly_gc_time_s"] = _read_olly_time_s(text, "GC time (s)")
    except ValueError:
        row["olly_gc_time_s"] = np.nan

    # Optional OCaml GC metrics from Gc.print_stat (present for binarytrees, absent for markbench).
    for metric in [
        "minor_collections",
        "major_collections",
        "compactions",
        "forced_major_collections",
        "minor_words",
        "promoted_words",
        "major_words",
        "top_heap_words",
        "heap_words",
        "live_words",
        "free_words",
        "fragments",
    ]:
        try:
            row[metric] = _read_metric_int(text, metric)
        except ValueError:
            row[metric] = np.nan

    row["promotion_rate"] = row["promoted_words"] / row["minor_words"] if row["minor_words"] else np.nan
    row["major_per_minor"] = row["major_collections"] / row["minor_collections"] if row["minor_collections"] else np.nan
    return row


def parse_logs(logs_dir: Path, pattern: str) -> pd.DataFrame:
    files = sorted(logs_dir.glob(pattern))
    if not files:
        raise SystemExit(f"No log files matching '{pattern}' in {logs_dir}")

    rows = [parse_log(p) for p in files]
    df = pd.DataFrame(rows).sort_values(["s", "o", "log_file"]).reset_index(drop=True)
    return df


def write_csv(df: pd.DataFrame, out_csv: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_csv, index=False, quoting=csv.QUOTE_MINIMAL)


def heatmap_grid(
    df: pd.DataFrame,
    metric: str,
    outpath: Path,
    title: str,
    logx: bool,
    logz: bool,
    agg: str,
) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError as e:
        raise SystemExit("matplotlib is required to generate plots. Install it in your environment.") from e

    grid = df.pivot_table(index="o", columns="s", values=metric, aggfunc=agg)
    grid = grid.sort_index(axis=0).sort_index(axis=1)

    xs = grid.columns.to_numpy(dtype=float)
    ys = grid.index.to_numpy(dtype=float)
    z = grid.to_numpy(dtype=float)

    xe = _edges(xs, log=logx)
    ye = _edges(ys, log=False)

    plt.figure()
    if logz:
        z_plot = np.where(z > 0, z, np.nan)
        pcm = plt.pcolormesh(xe, ye, np.log10(z_plot), shading="auto")
        plt.colorbar(pcm, label=f"log10({metric})")
    else:
        pcm = plt.pcolormesh(xe, ye, z, shading="auto")
        plt.colorbar(pcm, label=metric)

    if logx:
        plt.xscale("log")

    plt.xlabel("s (minor_heap_size)")
    plt.ylabel("o (space_overhead)")
    plt.title(title)
    plt.tight_layout()
    plt.savefig(outpath, dpi=200)
    plt.close()


def tradeoff_scatter(df: pd.DataFrame, outpath: Path) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError as e:
        raise SystemExit("matplotlib is required to generate plots. Install it in your environment.") from e

    plt.figure()

    s_vals = df["s"].to_numpy(dtype=float)
    log_s = np.log10(np.maximum(s_vals, 1.0))
    if np.nanmax(log_s) - np.nanmin(log_s) < 1e-9:
        sizes = np.full_like(log_s, 80.0)
    else:
        sizes = 20.0 + 180.0 * (log_s - np.nanmin(log_s)) / (np.nanmax(log_s) - np.nanmin(log_s))

    sc = plt.scatter(
        df["maxresident_mb"].to_numpy(dtype=float),
        df["elapsed_s"].to_numpy(dtype=float),
        c=df["o"].to_numpy(dtype=float),
        s=sizes,
        alpha=0.75,
    )
    cb = plt.colorbar(sc)
    cb.set_label("o (space_overhead)")

    plt.xlabel("Max RSS (MB)")
    plt.ylabel("Elapsed time (s)")
    plt.title("Time vs RSS tradeoff (color=o, size=log(s))")
    plt.tight_layout()
    plt.savefig(outpath, dpi=200)
    plt.close()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Parse benchmark logs, write gc_summary.csv, and produce elapsed/RSS heatmaps."
    )
    ap.add_argument("logs_dir", help="Directory containing binarytrees*.log files")
    ap.add_argument("--pattern", default=LOG_GLOB, help=f"Glob pattern inside logs_dir (default: {LOG_GLOB})")
    ap.add_argument("--outdir", default=None, help="Output directory (default: logs_dir)")
    ap.add_argument("--out-csv", default=None, help="CSV output path (default: <outdir>/gc_summary.csv)")
    ap.add_argument("--agg", default="median", choices=["median", "mean", "min", "max"], help="Aggregation for heatmaps")
    args = ap.parse_args()

    logs_dir = Path(args.logs_dir)
    if not logs_dir.is_dir():
        raise SystemExit(f"Not a directory: {logs_dir}")

    outdir = Path(args.outdir) if args.outdir else logs_dir
    outdir.mkdir(parents=True, exist_ok=True)
    out_csv = Path(args.out_csv) if args.out_csv else outdir / "gc_summary.csv"

    df = parse_logs(logs_dir, args.pattern)
    write_csv(df, out_csv)

    metric_specs = [
        ("olly_wall_time_s", "Wall time (s) over (s,o)", False),
        ("olly_gc_time_s", "GC time (s) over (s,o)", False),
    ]

    if "max_rss_mb" in df.columns and not df["max_rss_mb"].isna().all():
        metric_specs.append(("max_rss_mb", "Max RSS (MB) over (s,o)", False))

    # Legacy /usr/bin/time metrics (old logs without JSON sidecar)
    if "elapsed_s" in df.columns and not df["elapsed_s"].isna().all():
        metric_specs.append(("elapsed_s", "Elapsed time (s) over (s,o)", False))
    if "maxresident_mb" in df.columns and not df["maxresident_mb"].isna().all():
        metric_specs.append(("maxresident_mb", "Max RSS (MB) over (s,o)", False))

    for metric, title, logz in metric_specs:
        if metric not in df.columns or df[metric].isna().all():
            continue
        safe = re.sub(r"[^a-zA-Z0-9_]+", "_", metric).strip("_").lower()
        heatmap_grid(
            df,
            metric=metric,
            outpath=outdir / f"heatmap_{safe}.png",
            title=title,
            logx=True,
            logz=logz,
            agg=args.agg,
        )

    print(f"Wrote CSV: {out_csv}")
    print(f"Wrote plots to: {outdir}")


if __name__ == "__main__":
    main()
