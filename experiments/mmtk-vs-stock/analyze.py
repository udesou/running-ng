#!/usr/bin/env python3
"""Compare benchmark wall times and RSS across runtimes in a single image.

Reads *.log files produced by runbms with time_stats, writes gc_summary.csv,
and emits a single comparison.png with two panels:
  top    — wall time normalized to a baseline config (default: ocaml-4.14.3)
  bottom — max RSS in log scale (MMTk pre-allocates its fixed heap, so absolute
            RSS is misleading; the log scale keeps stock runtimes visible)

Usage:
    python3 experiments/mmtk-vs-stock/analyze.py <logs_dir>
    python3 experiments/mmtk-vs-stock/analyze.py <logs_dir> --normalize-to ""
"""
import argparse
import csv
import re
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd


LOG_GLOB = "*.log"
DEFAULT_BASELINE = "ocaml-4.14.3"

# Display order for legend / bar grouping — unknown configs fall after these.
CONFIG_ORDER = [
    "ocaml-4.14.3",
    "ocaml-trunk",
    "oxcaml-trunk",
    "mmtk/Immix",
    "mmtk/Sticky",
]


# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------

def _config_label(path: Path) -> str:
    """Return a short display label derived from the log filename."""
    name = path.name
    rt_m = re.search(r"^\w+\.\d+\.\d+\.((?:ocaml|oxcaml)-[^.]+(?:\.[0-9]+)*)\.", name)
    runtime = rt_m.group(1) if rt_m else "unknown"
    if "ocaml-mmtk" in runtime:
        if "mmtk_immix" in name:
            return "mmtk/Immix"
        if "mmtk_sticky" in name:
            return "mmtk/Sticky"
        return runtime
    return runtime


def _parse_filename(path: Path) -> tuple:
    name = path.name
    bm_m = re.match(r"^([^.]+)\.", name)
    benchmark = bm_m.group(1) if bm_m else name
    return benchmark, _config_label(path)


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

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


def parse_log(path: Path) -> Optional[dict]:
    if path.stat().st_size == 0:
        return None
    benchmark, config = _parse_filename(path)
    text = path.read_text()
    if not text.strip():
        return None
    row: dict = {"benchmark": benchmark, "config": config, "log_file": path.name}
    try:
        row["elapsed_s"] = _read_elapsed(text)
    except ValueError:
        row["elapsed_s"] = np.nan
    try:
        row["maxresident_mb"] = _read_maxresident_kb(text) / 1024.0
    except ValueError:
        row["maxresident_mb"] = np.nan
    # Flag failed runs (non-zero exit leaves only the wrapper's tiny RSS)
    row["failed"] = np.isnan(row["elapsed_s"]) or row.get("elapsed_s", 1) < 0.01
    return row


def parse_logs(logs_dir: Path) -> pd.DataFrame:
    files = sorted(logs_dir.glob(LOG_GLOB))
    if not files:
        raise SystemExit(f"No log files in {logs_dir}")
    rows = [r for r in (parse_log(p) for p in files) if r is not None]
    if not rows:
        raise SystemExit(f"No parseable data in {logs_dir}")
    df = pd.DataFrame(rows)
    df = df.sort_values(["benchmark", "config"]).reset_index(drop=True)
    return df


def write_csv(df: pd.DataFrame, out_csv: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_csv, index=False, quoting=csv.QUOTE_MINIMAL)


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def _ordered_configs(present: list) -> list:
    known = [c for c in CONFIG_ORDER if c in present]
    rest = sorted(c for c in present if c not in CONFIG_ORDER)
    return known + rest


def plot_comparison(
    df: pd.DataFrame,
    outpath: Path,
    normalize_to: Optional[str],
    agg: str,
) -> None:
    try:
        import matplotlib.pyplot as plt
        import matplotlib.ticker as mticker
    except ImportError as e:
        raise SystemExit("matplotlib is required: pip install matplotlib") from e

    # Drop failed rows before aggregating so a crashed run doesn't bias medians.
    clean = df[~df["failed"]].copy()

    configs = _ordered_configs(clean["config"].unique().tolist())
    benchmarks = sorted(clean["benchmark"].unique())
    n_b = len(benchmarks)
    n_c = len(configs)

    elapsed = clean.pivot_table(
        index="benchmark", columns="config", values="elapsed_s", aggfunc=agg
    ).reindex(index=benchmarks, columns=configs)

    rss = clean.pivot_table(
        index="benchmark", columns="config", values="maxresident_mb", aggfunc=agg
    ).reindex(index=benchmarks, columns=configs)

    elapsed_ylabel = "Wall time (s)"
    if normalize_to and normalize_to in elapsed.columns:
        elapsed = elapsed.div(elapsed[normalize_to], axis=0)
        elapsed_ylabel = f"Wall time  (ratio vs {normalize_to})"

    # ---- layout ----
    fig_w = max(14, n_b * 0.80)
    fig, (ax_t, ax_r) = plt.subplots(2, 1, figsize=(fig_w, 10))

    x = np.arange(n_b)
    width = 0.8 / n_c
    offsets = np.linspace(-(n_c - 1) / 2, (n_c - 1) / 2, n_c) * width
    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]

    for i, cfg in enumerate(configs):
        color = colors[i % len(colors)]
        if cfg in elapsed.columns:
            vals = elapsed[cfg].to_numpy(dtype=float)
            ax_t.bar(x + offsets[i], vals, width, label=cfg, color=color)
        if cfg in rss.columns:
            vals = rss[cfg].to_numpy(dtype=float)
            # Positive guard needed for log scale
            vals = np.where(vals > 0, vals, np.nan)
            ax_r.bar(x + offsets[i], vals, width, label=cfg, color=color)

    if normalize_to and normalize_to in configs:
        ax_t.axhline(1.0, color="black", linewidth=0.8, linestyle="--", alpha=0.5, zorder=0)

    for ax, ylabel in [(ax_t, elapsed_ylabel), (ax_r, "Max RSS (MB, log scale)")]:
        ax.set_xticks(x)
        ax.set_xticklabels(benchmarks, rotation=45, ha="right", fontsize=7)
        ax.set_ylabel(ylabel, fontsize=9)
        ax.legend(fontsize=8, loc="upper right", ncol=min(n_c, 3))
        ax.set_xlim(-0.6, n_b - 0.4)
        ax.tick_params(axis="y", labelsize=8)

    ax_r.set_yscale("log")
    ax_r.yaxis.set_major_formatter(mticker.ScalarFormatter())
    ax_r.annotate(
        "Note: MMTk RSS reflects pre-allocated fixed heap (8 GB ceiling), not live object footprint.",
        xy=(0, 0), xycoords="axes fraction",
        xytext=(0, -0.30), textcoords="axes fraction",
        fontsize=6.5, color="#666666",
    )

    run_label = Path(outpath).parent.name
    fig.suptitle(f"OCaml runtime comparison — {run_label}", fontsize=11, y=1.01)
    plt.tight_layout()
    plt.savefig(outpath, dpi=200, bbox_inches="tight")
    plt.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Compare benchmark results across runtimes in a single image."
    )
    ap.add_argument("logs_dir", help="Directory containing *.log files from runbms")
    ap.add_argument(
        "--outdir", default=None,
        help="Output directory for CSV and PNG (default: logs_dir)",
    )
    ap.add_argument(
        "--normalize-to",
        default=DEFAULT_BASELINE,
        metavar="CONFIG",
        help=(
            f"Config label to use as the 1.0 baseline for elapsed time "
            f"(default: {DEFAULT_BASELINE!r}). Pass empty string to show "
            "absolute seconds instead."
        ),
    )
    ap.add_argument(
        "--agg", default="median",
        choices=["median", "mean", "min", "max"],
        help="Aggregation over multiple invocations (default: median)",
    )
    ap.add_argument("--out-csv", default=None)
    args = ap.parse_args()

    logs_dir = Path(args.logs_dir)
    if not logs_dir.is_dir():
        raise SystemExit(f"Not a directory: {logs_dir}")

    outdir = Path(args.outdir) if args.outdir else logs_dir
    outdir.mkdir(parents=True, exist_ok=True)
    out_csv = Path(args.out_csv) if args.out_csv else outdir / "gc_summary.csv"

    df = parse_logs(logs_dir)
    write_csv(df, out_csv)
    print(f"Wrote CSV : {out_csv}")

    normalize_to = args.normalize_to or None
    plot_path = outdir / "comparison.png"
    plot_comparison(df, plot_path, normalize_to=normalize_to, agg=args.agg)
    print(f"Wrote plot: {plot_path}")


if __name__ == "__main__":
    main()
