"""
Proof-of-concept: representative benchmark selection via PCA + k-means.

Usage (from repo root, with the project venv active):
    python experiments/clustering/poc.py \
        --logs gc-sweep-logs/turing-2026-06-23-Tue-221257 \
        --runtime 5.4.1 \
        --out experiments/clustering/results/poc

Outputs written to --out/:
    feature_overview.png        per-feature bar chart across all benchmarks
    correlation_heatmap.png     feature correlation matrix
    pca_scree.png               variance explained per PC
    pca_loading_heatmap.png     feature contributions to top PCs
    clustering_scatter.png      2-D PCA scatter with k-means cluster colours
    cluster_composition.png     which benchmarks landed in each cluster
    representative_suite.png    bar chart of the selected benchmarks only
    representative_suite.yml    YAML snippet ready for a new experiment config
    run_log.txt                 step-by-step log
"""

import argparse
import json
import re
import sys
import textwrap
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import pandas as pd
import seaborn as sns
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

# ---------------------------------------------------------------------------
# Filename parser — handles the re_par / md_par / pin_lavyek token layout
# ---------------------------------------------------------------------------

FNAME_RE = re.compile(
    r"^(?P<benchmark>[a-z0-9_]+)"
    r"\.(?P<iter>\d+)"
    r"\.(?P<sub_iter>\d+)"
    r"\.(?:ocaml|oxcaml)-(?P<ocaml>[\w.-]+?)"
    r"\.perf_grp(?P<perf_grp>\d+)"
    r"(?:\.[a-z0-9_]+-\d+)*"
    r"(?:\.[a-z0-9_]+)*"
    r"(?:\.macro-(?P<macro_repo>[a-z0-9-]+))?"
    r"\.log$"
)

KNOWN_FLAG_SUFFIXES = ("fp-flambda", "flambda", "fp")


def _split_ocaml(s):
    for suf in KNOWN_FLAG_SUFFIXES:
        if s.endswith("-" + suf):
            return s[: -(len(suf) + 1)], suf
    return s, "baseline"


def parse_filename(name):
    m = FNAME_RE.match(name)
    if not m:
        return None
    g = m.groupdict()
    version, flags = _split_ocaml(g["ocaml"])
    return {
        "benchmark": g["benchmark"],
        "iter": int(g["iter"]),
        "sub_iter": int(g["sub_iter"]),
        "version": version,
        "flags": flags,
        "perf_grp": int(g["perf_grp"]),
        "macro_repo": g["macro_repo"],
    }


# ---------------------------------------------------------------------------
# Sidecar reader
# ---------------------------------------------------------------------------

def _read_ndjson(path):
    try:
        raw = path.read_bytes()
        lines = [l.strip() for l in raw.decode("utf-8", errors="replace").splitlines() if l.strip()]
        return [json.loads(l) for l in lines]
    except Exception:
        return []


def find_sidecars(log_path):
    stem = log_path.name
    if stem.endswith(".log"):
        base = stem[:-4]
    else:
        return {}
    found = {}
    for tool in ("olly", "perf"):
        cand = log_path.parent / f"{tool}_{base}.json"
        if cand.exists():
            found[tool] = cand
    return found


def flatten_olly(olly):
    if not olly:
        return {}
    row = {
        "wall_time_s":      olly.get("wall_time", np.nan),
        "cpu_time_s":       olly.get("cpu_time", np.nan),
        "gc_time_s":        olly.get("gc_time", np.nan),
        "gc_overhead_pct":  olly.get("gc_overhead", np.nan),
        "max_rss_mb":       (olly.get("max_rss_kb", np.nan) or np.nan) / 1024.0,
        "mean_latency_ms":  olly.get("mean_latency", np.nan),
        "p95_latency_ms":   (olly.get("distr_latency") or {}).get("95.0000", np.nan),
        "p99_latency_ms":   (olly.get("distr_latency") or {}).get("99.0000", np.nan),
        "max_latency_ms":   olly.get("max_latency", np.nan),
    }
    alloc = olly.get("allocations") or {}
    row["total_heap_Mw"] = (alloc.get("total_heap", np.nan) or np.nan) / 1e6
    row["minor_heap_Mw"] = (alloc.get("minor_heap", np.nan) or np.nan) / 1e6
    row["promoted_pct"]  = alloc.get("promoted_pct", np.nan)
    col = olly.get("collections") or {}
    row["minor_colls"]   = col.get("minor", np.nan)
    row["major_colls"]   = col.get("major", np.nan)
    row["compactions"]   = col.get("compactions", np.nan)
    m = row["minor_colls"]
    M = row["major_colls"]
    row["major_per_minor"] = (M / m) if (m and M and m > 0) else np.nan
    return row


def flatten_perf(records):
    row = {}
    for entry in (records or []):
        event = entry.get("event")
        if not event:
            continue
        try:
            row[f"perf_{event}"] = float(entry["counter-value"])
        except (KeyError, ValueError, TypeError):
            pass
        mv = entry.get("metric-value")
        mu = entry.get("metric-unit")
        if mv is not None and mu:
            safe = re.sub(r"[^a-zA-Z0-9_]+", "_", mu).strip("_").lower()
            if safe:
                try:
                    row[f"perf_{event}_{safe}"] = float(mv)
                except (ValueError, TypeError):
                    pass
    return row


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_data(logs_dir, runtime_version):
    logs_dir = Path(logs_dir)
    rows = []
    for log_path in sorted(logs_dir.glob("*.log")):
        meta = parse_filename(log_path.name)
        if meta is None or meta["version"] != runtime_version:
            continue
        sidecars = find_sidecars(log_path)
        olly_records = _read_ndjson(sidecars["olly"]) if "olly" in sidecars else []
        perf_records  = _read_ndjson(sidecars["perf"])  if "perf"  in sidecars else []
        # align invocations
        n = max(len(olly_records), len(perf_records), 1)
        for i in range(n):
            row = {**meta, "invocation": i}
            if i < len(olly_records):
                row.update(flatten_olly(olly_records[i]))
            if i < len(perf_records):
                row.update(flatten_perf(perf_records[i]))
            rows.append(row)
    df = pd.DataFrame(rows)
    return df


def aggregate(df, feature_cols):
    """Median across invocations per benchmark."""
    return (
        df.groupby("benchmark")[feature_cols]
        .median()
        .reset_index()
        .set_index("benchmark")
    )


# ---------------------------------------------------------------------------
# Feature selection
# ---------------------------------------------------------------------------

OLLY_FEATURES = [
    "wall_time_s", "cpu_time_s", "gc_time_s", "gc_overhead_pct",
    "max_rss_mb",
    "mean_latency_ms", "p95_latency_ms", "p99_latency_ms", "max_latency_ms",
    "total_heap_Mw", "minor_heap_Mw", "promoted_pct",
    "minor_colls", "major_colls", "compactions", "major_per_minor",
]

PERF_FEATURES = [
    "perf_task-clock", "perf_page-faults", "perf_cycles", "perf_instructions",
    "perf_instructions_insn_per_cycle",
]

# Friendly display names for plot labels
FEATURE_LABELS = {
    "wall_time_s":       "Wall time (s)",
    "cpu_time_s":        "CPU time (s)",
    "gc_time_s":         "GC time (s)",
    "gc_overhead_pct":   "GC overhead (%)",
    "max_rss_mb":        "Max RSS (MB)",
    "mean_latency_ms":   "Mean GC pause (ms)",
    "p95_latency_ms":    "p95 GC pause (ms)",
    "p99_latency_ms":    "p99 GC pause (ms)",
    "max_latency_ms":    "Max GC pause (ms)",
    "total_heap_Mw":     "Total alloc (Mw)",
    "minor_heap_Mw":     "Minor alloc (Mw)",
    "promoted_pct":      "Promoted (%)",
    "minor_colls":       "Minor collections",
    "major_colls":       "Major collections",
    "compactions":       "Compactions",
    "major_per_minor":   "Major / minor ratio",
    "perf_task-clock":           "CPU-ms (perf)",
    "perf_page-faults":          "Page faults",
    "perf_cycles":               "CPU cycles",
    "perf_instructions":         "Instructions",
    "perf_instructions_insn_per_cycle": "IPC",
}

# Suite membership for the YAML output
SUITE_MAP = {
    "menhir_ocamly":        "macro-menhir-monorepo",
    "menhir_sql_parser":    "macro-menhir-monorepo",
    "menhir_sysver":        "macro-menhir-monorepo",
    "cpdf_merge":           "macro-cpdf-monorepo",
    "cpdf_blacktext":       "macro-cpdf-monorepo",
    "cpdf_scale":           "macro-cpdf-monorepo",
    "cpdf_squeeze":         "macro-cpdf-monorepo",
    "alt_ergo_fill":        "macro-alt-ergo-monorepo",
    "alt_ergo_yyll":        "macro-alt-ergo-monorepo",
    "alt_ergo_unsat_smt2":  "macro-alt-ergo-monorepo",
    "frama_c_eva_t":        "macro-frama-c-monorepo",
    "frama_c_eva_sqlite":   "macro-frama-c-monorepo",
    "goblint":              "macro-goblint-monorepo",
    "coqc_corelib_stress":  "macro-coq-monorepo",
    "devkit_htmlstream":    "macro-devkit",
    "devkit_stre":          "macro-devkit",
    "devkit_network":       "macro-devkit",
    "devkit_gzip":          "macro-devkit",
    "irmin_mem_rw":         "macro-irmin",
    "ocamlformat_rocq":     "macro-ocamlformat",
    "test_decompress":      "macro-decompress",
    "eio_fiber_stream":     "macro-eio",
    "sedlex_tokenize":      "macro-sedlex",
    "ydump_repeat":         "macro-yojson",
    "zarith_pi":            "macro-zarith",
    "owl_gc":               "macro-owl",
    "ocamlc_self_compile":  "macro-ocamlc-self-compile",
    "liq_parse_typecheck":  "macro-liquidsoap-lang",
    "jsoo":                 "macro-jsoo",
    "liq_video_frames_pool":"macro-liq-video-frames",
    "pplacer_testsuite":    "macro-pplacer",
}


# ---------------------------------------------------------------------------
# Plotting helpers
# ---------------------------------------------------------------------------

PALETTE = sns.color_palette("tab10")


def savefig(fig, path, log):
    fig.savefig(path, bbox_inches="tight", dpi=150)
    plt.close(fig)
    log(f"  saved {path}")


# ---------------------------------------------------------------------------
# Plot 1 — per-feature bar charts (one figure, grid of subplots)
# ---------------------------------------------------------------------------

def plot_feature_overview(feat_df, outdir, log):
    features = [f for f in feat_df.columns]
    n = len(features)
    ncols = 3
    nrows = (n + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(ncols * 7, nrows * 3.5))
    axes = axes.flatten()
    benchmarks = feat_df.index.tolist()
    x = np.arange(len(benchmarks))

    for i, feat in enumerate(features):
        ax = axes[i]
        vals = feat_df[feat].values
        bars = ax.bar(x, vals, color=PALETTE[i % len(PALETTE)], edgecolor="none", alpha=0.85)
        ax.set_xticks(x)
        ax.set_xticklabels(benchmarks, rotation=60, ha="right", fontsize=6.5)
        ax.set_title(FEATURE_LABELS.get(feat, feat), fontsize=9, fontweight="bold")
        ax.set_ylabel(FEATURE_LABELS.get(feat, feat), fontsize=7)
        ax.tick_params(axis="y", labelsize=7)
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", linestyle="--", alpha=0.4)

    for j in range(i + 1, len(axes)):
        axes[j].set_visible(False)

    fig.suptitle(f"Feature values across benchmarks (ocaml-{feat_df.attrs.get('runtime','?')})",
                 fontsize=12, fontweight="bold", y=1.01)
    fig.tight_layout()
    savefig(fig, outdir / "feature_overview.png", log)


# ---------------------------------------------------------------------------
# Plot 2 — feature correlation heatmap
# ---------------------------------------------------------------------------

def plot_correlation(feat_df, outdir, log):
    corr = feat_df.corr()
    labels = [FEATURE_LABELS.get(c, c) for c in corr.columns]
    mask = np.triu(np.ones_like(corr, dtype=bool), k=1)
    fig, ax = plt.subplots(figsize=(12, 10))
    sns.heatmap(
        corr, mask=mask, annot=True, fmt=".1f", cmap="RdBu_r",
        vmin=-1, vmax=1, linewidths=0.3, ax=ax,
        xticklabels=labels, yticklabels=labels,
        annot_kws={"size": 6},
    )
    ax.set_title("Feature correlation matrix", fontsize=12, fontweight="bold")
    plt.xticks(rotation=45, ha="right", fontsize=7)
    plt.yticks(fontsize=7)
    fig.tight_layout()
    savefig(fig, outdir / "correlation_heatmap.png", log)


# ---------------------------------------------------------------------------
# PCA + scree + loadings
# ---------------------------------------------------------------------------

def run_pca(X_scaled, feature_names, outdir, log):
    pca = PCA()
    pca.fit(X_scaled)
    ev = pca.explained_variance_ratio_

    # Scree
    cumev = np.cumsum(ev)
    n90 = int(np.searchsorted(cumev, 0.90)) + 1
    log(f"  PCA: {n90} components explain ≥90% variance (total {len(ev)} features)")

    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    axes[0].bar(range(1, len(ev) + 1), ev * 100, color="#4C72B0", edgecolor="none", alpha=0.85)
    axes[0].set_xlabel("Principal component")
    axes[0].set_ylabel("Variance explained (%)")
    axes[0].set_title("Scree plot — individual variance")
    axes[0].axvline(n90, color="crimson", linestyle="--", linewidth=1.2, label=f"n={n90} (90% var)")
    axes[0].legend(fontsize=8)
    axes[0].spines[["top", "right"]].set_visible(False)

    axes[1].plot(range(1, len(cumev) + 1), cumev * 100, "o-", color="#4C72B0", markersize=4)
    axes[1].axhline(90, color="crimson", linestyle="--", linewidth=1.2, label="90%")
    axes[1].axvline(n90, color="crimson", linestyle="--", linewidth=1.2)
    axes[1].set_xlabel("Number of components")
    axes[1].set_ylabel("Cumulative variance (%)")
    axes[1].set_title("Scree plot — cumulative variance")
    axes[1].legend(fontsize=8)
    axes[1].spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    savefig(fig, outdir / "pca_scree.png", log)

    # Loading heatmap (top 5 PCs)
    n_show = min(5, X_scaled.shape[1])
    loadings = pd.DataFrame(
        pca.components_[:n_show].T,
        index=[FEATURE_LABELS.get(f, f) for f in feature_names],
        columns=[f"PC{i+1}\n({ev[i]*100:.1f}%)" for i in range(n_show)],
    )
    fig, ax = plt.subplots(figsize=(8, max(5, len(feature_names) * 0.45)))
    sns.heatmap(
        loadings, annot=True, fmt=".2f", cmap="RdBu_r",
        vmin=-1, vmax=1, linewidths=0.3, ax=ax, annot_kws={"size": 7},
    )
    ax.set_title("PCA loadings — top components", fontsize=11, fontweight="bold")
    plt.xticks(fontsize=8)
    plt.yticks(fontsize=7, rotation=0)
    fig.tight_layout()
    savefig(fig, outdir / "pca_loading_heatmap.png", log)

    return pca, n90


# ---------------------------------------------------------------------------
# k-means — silhouette sweep + clustering
# ---------------------------------------------------------------------------

def run_kmeans(X_pca, n_benchmarks, outdir, log):
    k_min = 4
    k_max = min(10, n_benchmarks - 1)
    scores = {}
    for k in range(k_min, k_max + 1):
        km = KMeans(n_clusters=k, random_state=42, n_init=20)
        labels = km.fit_predict(X_pca)
        scores[k] = silhouette_score(X_pca, labels)
    best_k = max(scores, key=scores.get)
    log(f"  k-means: best k={best_k} (silhouette={scores[best_k]:.3f})")

    # silhouette plot
    fig, ax = plt.subplots(figsize=(7, 4))
    ks = list(scores.keys())
    sc = [scores[k] for k in ks]
    ax.plot(ks, sc, "o-", color="#4C72B0", markersize=6, linewidth=1.5)
    ax.axvline(best_k, color="crimson", linestyle="--", linewidth=1.2, label=f"best k={best_k}")
    ax.set_xlabel("Number of clusters k")
    ax.set_ylabel("Silhouette score")
    ax.set_title("k-means: silhouette score vs k", fontsize=11, fontweight="bold")
    ax.legend(fontsize=9)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    fig.tight_layout()
    savefig(fig, outdir / "kmeans_silhouette.png", log)

    km_final = KMeans(n_clusters=best_k, random_state=42, n_init=20)
    labels = km_final.fit_predict(X_pca)
    return km_final, labels, best_k


# ---------------------------------------------------------------------------
# Plot — 2-D PCA scatter coloured by cluster
# ---------------------------------------------------------------------------

def plot_cluster_scatter(pca, X_scaled, labels, benchmarks, best_k, outdir, log):
    coords = pca.transform(X_scaled)[:, :2]
    ev = pca.explained_variance_ratio_

    fig, ax = plt.subplots(figsize=(11, 8))
    cluster_colors = matplotlib.colormaps["tab10"]

    for k in range(best_k):
        mask = labels == k
        ax.scatter(
            coords[mask, 0], coords[mask, 1],
            color=cluster_colors(k), s=110, label=f"Cluster {k+1}",
            edgecolors="white", linewidths=0.6, zorder=3,
        )

    for i, name in enumerate(benchmarks):
        ax.annotate(
            name, (coords[i, 0], coords[i, 1]),
            fontsize=6.5, ha="left", va="bottom",
            xytext=(4, 4), textcoords="offset points",
            color="#333333",
        )

    ax.set_xlabel(f"PC1 ({ev[0]*100:.1f}% var)", fontsize=10)
    ax.set_ylabel(f"PC2 ({ev[1]*100:.1f}% var)", fontsize=10)
    ax.set_title("PCA embedding — k-means clusters", fontsize=12, fontweight="bold")
    ax.legend(fontsize=9, markerscale=1.2)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(linestyle="--", alpha=0.3)
    fig.tight_layout()
    savefig(fig, outdir / "clustering_scatter.png", log)


# ---------------------------------------------------------------------------
# Plot — cluster composition strip chart
# ---------------------------------------------------------------------------

def plot_cluster_composition(labels, benchmarks, best_k, outdir, log):
    cluster_colors = matplotlib.colormaps["tab10"]
    clusters = {k: [] for k in range(best_k)}
    for bench, lbl in zip(benchmarks, labels):
        clusters[lbl].append(bench)

    fig, ax = plt.subplots(figsize=(12, max(4, best_k * 1.4)))
    for k in range(best_k):
        members = clusters[k]
        for j, name in enumerate(members):
            ax.text(
                j, k, name,
                ha="center", va="center", fontsize=8,
                color="white", fontweight="bold",
                bbox=dict(
                    boxstyle="round,pad=0.35",
                    facecolor=cluster_colors(k),
                    edgecolor="none", alpha=0.88,
                ),
            )
        ax.axhline(k, color="#cccccc", linewidth=0.5, zorder=0)

    ax.set_yticks(range(best_k))
    ax.set_yticklabels([f"Cluster {k+1}  ({len(clusters[k])} benches)" for k in range(best_k)],
                       fontsize=9)
    ax.set_xlim(-0.6, max(len(v) for v in clusters.values()) - 0.4)
    ax.set_xticks([])
    ax.set_title("Cluster composition — all benchmarks", fontsize=12, fontweight="bold")
    ax.spines[["top", "right", "bottom"]].set_visible(False)
    fig.tight_layout()
    savefig(fig, outdir / "cluster_composition.png", log)
    return clusters


# ---------------------------------------------------------------------------
# Representative selection — closest to centroid in scaled feature space
# ---------------------------------------------------------------------------

def select_representatives(km, X_pca, labels, benchmarks, best_k, log):
    centroids = km.cluster_centers_
    reps = {}
    for k in range(best_k):
        indices = np.where(labels == k)[0]
        dists = np.linalg.norm(X_pca[indices] - centroids[k], axis=1)
        closest_idx = indices[np.argmin(dists)]
        members = [benchmarks[i] for i in indices]
        reps[k] = {
            "benchmark": benchmarks[closest_idx],
            "dist": float(dists.min()),
            "cluster_size": len(indices),
            "members": members,
        }
        log(f"  Cluster {k+1}: representative = {benchmarks[closest_idx]}"
            f"  (dist={dists.min():.3f}, cluster_size={len(indices)}, members={members})")
    return reps


# ---------------------------------------------------------------------------
# Plot — representative benchmarks: feature radar-style bar chart
# ---------------------------------------------------------------------------

def plot_representative_suite(reps, feat_df, best_k, outdir, log):
    rep_names = [reps[k]["benchmark"] for k in range(best_k)]
    rep_df = feat_df.loc[rep_names].copy()

    # Normalise each feature to [0, 1] across ALL benchmarks for fair comparison
    norm_df = (rep_df - feat_df.min()) / (feat_df.max() - feat_df.min() + 1e-9)

    features = norm_df.columns.tolist()
    x = np.arange(len(features))
    width = 0.8 / best_k
    cluster_colors = matplotlib.colormaps["tab10"]

    fig, ax = plt.subplots(figsize=(max(14, len(features) * 0.9), 5))
    for i, (k, bench) in enumerate(zip(range(best_k), rep_names)):
        offset = (i - best_k / 2 + 0.5) * width
        vals = norm_df.loc[bench].values
        ax.bar(x + offset, vals, width=width * 0.9,
               label=f"C{k+1}: {bench}", color=cluster_colors(k),
               edgecolor="none", alpha=0.85)

    ax.set_xticks(x)
    ax.set_xticklabels([FEATURE_LABELS.get(f, f) for f in features],
                       rotation=45, ha="right", fontsize=7.5)
    ax.set_ylabel("Normalised value (0–1 across all benchmarks)", fontsize=9)
    ax.set_title("Representative benchmark suite — feature profile", fontsize=12, fontweight="bold")
    ax.legend(fontsize=8, ncol=min(best_k, 4))
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", linestyle="--", alpha=0.35)
    fig.tight_layout()
    savefig(fig, outdir / "representative_suite.png", log)


# ---------------------------------------------------------------------------
# YAML output
# ---------------------------------------------------------------------------

def write_yaml(reps, best_k, runtime, outdir, log):
    from collections import defaultdict
    suites = defaultdict(list)
    for k in range(best_k):
        bench = reps[k]["benchmark"]
        suite = SUITE_MAP.get(bench, "macro-unknown")
        suites[suite].append(bench)

    lines = [
        f"# Representative suite — generated by experiments/clustering/poc.py",
        f"# Runtime: ocaml-{runtime}  |  k-means k={best_k}",
        f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"#",
        f"# Cluster representatives (one per cluster, closest to centroid):",
    ]
    for k in range(best_k):
        r = reps[k]
        lines.append(f"#   Cluster {k+1}: {r['benchmark']}"
                     f"  (size={r['cluster_size']}, dist={r['dist']:.3f})")
    lines.append("")
    lines.append("benchmarks:")
    for suite, benches in sorted(suites.items()):
        lines.append(f"  {suite}:")
        for b in benches:
            lines.append(f"    - {b}")

    yaml_path = outdir / "representative_suite.yml"
    yaml_path.write_text("\n".join(lines) + "\n")
    log(f"  saved {yaml_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--logs",    required=True, help="Path to a gc-sweep-logs/<run>/ directory")
    parser.add_argument("--runtime", default="5.4.1", help="OCaml version string (default: 5.4.1)")
    parser.add_argument("--out",     default="experiments/clustering/results/poc", help="Output directory")
    args = parser.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    log_lines = []

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        log_lines.append(line)

    log(f"=== Benchmark clustering PoC ===")
    log(f"Logs dir : {args.logs}")
    log(f"Runtime  : ocaml-{args.runtime}")
    log(f"Output   : {outdir}")

    # ------------------------------------------------------------------
    # Stage 1: load
    # ------------------------------------------------------------------
    log("\n[1/6] Loading benchmark data...")
    df = load_data(args.logs, args.runtime)
    if df.empty:
        print(f"ERROR: no data found for runtime {args.runtime} in {args.logs}")
        sys.exit(1)
    log(f"  Loaded {len(df)} invocation rows, {df['benchmark'].nunique()} benchmarks")

    # ------------------------------------------------------------------
    # Stage 2: build feature matrix
    # ------------------------------------------------------------------
    log("\n[2/6] Building feature matrix...")
    all_features = OLLY_FEATURES + [f for f in PERF_FEATURES if f in df.columns]
    present = [f for f in all_features if f in df.columns]
    feat_df = aggregate(df, present)
    feat_df.attrs["runtime"] = args.runtime

    # drop columns that are all-NaN or zero-variance
    feat_df = feat_df.dropna(axis=1, how="all")
    feat_df = feat_df.loc[:, feat_df.std() > 1e-9]
    # drop rows (benchmarks) with any NaN in remaining features
    n_before = len(feat_df)
    feat_df = feat_df.dropna()
    if len(feat_df) < n_before:
        log(f"  Dropped {n_before - len(feat_df)} benchmarks with incomplete data")
    log(f"  Feature matrix: {feat_df.shape[0]} benchmarks × {feat_df.shape[1]} features")
    log(f"  Features: {', '.join(feat_df.columns.tolist())}")

    benchmarks  = feat_df.index.tolist()
    feature_cols = feat_df.columns.tolist()
    X_raw = feat_df.values.astype(float)

    # ------------------------------------------------------------------
    # Stage 3: plots — feature overview + correlation
    # ------------------------------------------------------------------
    log("\n[3/6] Plotting feature overview and correlation...")
    plot_feature_overview(feat_df, outdir, log)
    plot_correlation(feat_df, outdir, log)

    # ------------------------------------------------------------------
    # Stage 4: PCA
    # ------------------------------------------------------------------
    log("\n[4/6] Running PCA...")
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_raw)
    pca, n90 = run_pca(X_scaled, feature_cols, outdir, log)
    # Project into the n90-component PCA space for clustering (decorrelated features)
    X_pca = pca.transform(X_scaled)[:, :n90]
    log(f"  Clustering in PCA space: {X_pca.shape[1]} decorrelated components")

    # ------------------------------------------------------------------
    # Stage 5: k-means clustering
    # ------------------------------------------------------------------
    log("\n[5/6] Running k-means (silhouette sweep)...")
    km, labels, best_k = run_kmeans(X_pca, len(benchmarks), outdir, log)
    plot_cluster_scatter(pca, X_scaled, labels, benchmarks, best_k, outdir, log)
    clusters = plot_cluster_composition(labels, benchmarks, best_k, outdir, log)

    # ------------------------------------------------------------------
    # Stage 6: representative selection + final chart
    # ------------------------------------------------------------------
    log("\n[6/6] Selecting representatives and plotting final suite...")
    reps = select_representatives(km, X_pca, labels, benchmarks, best_k, log)
    plot_representative_suite(reps, feat_df, best_k, outdir, log)
    write_yaml(reps, best_k, args.runtime, outdir, log)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    log("\n=== Summary ===")
    log(f"  Input benchmarks   : {len(benchmarks)}")
    log(f"  Features used      : {len(feature_cols)}")
    log(f"  PCA 90% variance   : {n90} components")
    log(f"  Clusters (k)       : {best_k}")
    log(f"  Representatives    :")
    for k in range(best_k):
        log(f"    Cluster {k+1}: {reps[k]['benchmark']}  (cluster_size={reps[k]['cluster_size']})")
    log(f"\nOutputs in: {outdir}/")

    (outdir / "run_log.txt").write_text("\n".join(log_lines) + "\n")


if __name__ == "__main__":
    main()
