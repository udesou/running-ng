# Benchmark Clustering Pipeline — Architecture

## Goal

Select a minimal, representative subset of the OCaml macro-benchmark suite by applying
dimensionality reduction and clustering to the GC/performance feature vectors of each
benchmark.  The approach mirrors the DaCapo paper (OOPSLA 2006, §4) which used PCA to
justify its Java benchmark selection.

The pipeline is designed to be:

- **Technique-agnostic** — PCA, k-means, UMAP, and future methods plug in via a uniform
  interface without touching the rest of the code.
- **Feature-extensible** — new observability columns (e.g. live-object counts from a
  future olly probe) appear in the feature matrix automatically; no pipeline code changes
  are required.
- **Benchmark-extensible** — adding benchmarks to `macro_base.yml` and re-running the
  harness is the only prerequisite for an updated selection.
- **Reproducible** — every stage writes its output to `data/` or `results/` with a
  timestamp; a run log lands in `logs/`.

---

## Directory layout

```
experiments/clustering/
├── ARCHITECTURE.md          ← this file
├── run_pipeline.py          ← CLI entry-point (orchestrates all stages)
├── pipeline/
│   ├── __init__.py
│   ├── extract.py           ← Stage 1: load logs → feature matrix
│   ├── reduce.py            ← Stage 2: normalise + apply DR technique
│   ├── cluster.py           ← Stage 3: cluster the reduced space
│   ├── select.py            ← Stage 4: pick one representative per cluster
│   ├── visualize.py         ← Stage 5: plots (scatter, scree, heatmap)
│   └── report.py            ← Stage 6: write YAML snippet + markdown summary
├── data/                    ← intermediate artefacts (gitignored CSVs)
├── results/                 ← final outputs (plots, YAML, summary markdown)
└── logs/                    ← per-run step logs
```

---

## Stage 1 — Feature extraction (`pipeline/extract.py`)

**Input:** path to a `gc-sweep-logs/<run>/` directory (or a list of runs).

**Process:**
1. Call `notebooks/macrobench_loader.load_macro_dataframe(logs_dir)` to get the tidy
   per-invocation DataFrame.  This already handles olly + perf sidecar parsing.
2. Aggregate across invocations with `aggregate_invocations(df, metrics, group_cols=["benchmark"])`.
   Use median as the central value (small N; CLT does not apply).
3. Drop columns that are not observability metrics (log_file, iter, version, flags, etc.)
   and any column that is >50 % NaN across benchmarks (e.g. perf groups that were not
   collected in this run).
4. Output: a `(n_benchmarks × n_features)` DataFrame indexed by benchmark name, saved to
   `data/features_<timestamp>.csv`.

**Currently available features** (from olly + perf_grp1; all present in the latest runs):

| Column | Source | Interpretation |
|---|---|---|
| `olly_wall_time_s` | olly | total wall time |
| `olly_cpu_time_s` | olly | CPU time (proxy for compute intensity) |
| `olly_gc_time_s` | olly | time in GC |
| `olly_gc_overhead_pct` | olly | gc_time / wall_time — GC pressure |
| `max_rss_mb` | olly | peak resident set size |
| `olly_mean_latency_ms` | olly | mean GC pause |
| `olly_p95_latency_ms` | olly | 95th-pctile GC pause — tail latency |
| `olly_p99_latency_ms` | olly | 99th-pctile GC pause |
| `olly_max_latency_ms` | olly | worst-case GC pause |
| `total_heap_words` | olly | total allocation volume |
| `minor_words` | olly | minor allocation volume |
| `promoted_pct` | olly | fraction of minor that survived — tenuring rate |
| `minor_collections` | olly | minor GC frequency |
| `major_collections` | olly | major GC frequency |
| `major_per_minor` | olly | major/minor ratio — derived |
| `compactions` | olly | compaction count |
| `perf_task-clock` | perf | CPU-ms consumed |
| `perf_page-faults` | perf | page fault count |
| `perf_cycles` | perf | CPU cycle count |
| `perf_instructions` | perf | instruction count |
| `perf_instructions_insn_per_cycle` | perf | IPC — pipeline efficiency |

**Future features** (no pipeline changes needed when added to olly):
- Live object counts / heap occupancy at major GC (future olly probe)
- Compactor-triggered compaction bytes
- Per-domain GC stats (already in the olly JSON under `domain_stats`)

**Design note:** `extract.py` exposes a single function:

```python
def extract_features(
    logs_dir: str | Path,
    *,
    nan_threshold: float = 0.5,   # drop columns with >50% NaN
    center: str = "median",
) -> pd.DataFrame:
    ...
```

Callers pass a logs directory; the function returns a clean `(benchmarks × features)` matrix.
Adding a new observability column to the olly JSON automatically makes it appear in the output.

---

## Stage 2 — Normalisation + dimensionality reduction (`pipeline/reduce.py`)

**Input:** feature matrix from Stage 1.

**Process:**
1. Drop constant and near-zero-variance columns (std < 1e-6 after z-scoring).
2. Z-score normalise (subtract mean, divide by std) so features on wildly different scales
   (wall_time in seconds vs instruction counts in 10^11) are comparable.
3. Apply the selected DR technique.

**Technique registry** (the extensible part):

```python
# Each technique is a callable:
#   fit_transform(X: np.ndarray, **kwargs) -> np.ndarray   # n_benchmarks × n_components
# The output is the low-dimensional embedding used for clustering and visualisation.

TECHNIQUES: dict[str, Callable] = {
    "pca":  _pca,      # sklearn PCA; also returns explained_variance_ratio_
    "umap": _umap,     # umap-learn UMAP
    "tsne": _tsne,     # sklearn TSNE (visualisation only — stochastic, not for clustering)
    "ica":  _ica,      # sklearn FastICA
}
```

To add a new technique, register a function in `TECHNIQUES` — nothing else changes.

**PCA specifics:**
- Run PCA with `n_components = min(n_benchmarks, n_features)`.
- Retain components that explain ≥5% of variance individually, or enough components to reach
  90% cumulative explained variance, whichever gives fewer components.
- The scree plot (Stage 5) shows the full variance-explained curve.
- The loading matrix (features × components) is saved to `data/pca_loadings_<timestamp>.csv`
  for interpretability.

**Output:** `(n_benchmarks × n_components)` embedding array + metadata dict
(technique name, n_components, explained_variance if available).

---

## Stage 3 — Clustering (`pipeline/cluster.py`)

**Input:** embedding from Stage 2.

**Process:**
Apply the selected clustering method to the embedding.

**Clusterer registry:**

```python
CLUSTERERS: dict[str, Callable] = {
    "kmeans":       _kmeans,        # sklearn KMeans
    "agglomerative": _agglomerative, # sklearn AgglomerativeClustering (Ward linkage)
    "dbscan":       _dbscan,        # sklearn DBSCAN (density-based; k not required)
    "gmm":          _gmm,           # sklearn GaussianMixture
}
```

**k selection:**
- For methods that require k: sweep k = 2 … min(10, n_benchmarks-1), compute silhouette
  score for each, pick the k with the highest score.  The silhouette-vs-k plot is written
  to `results/`.
- For DBSCAN: eps is estimated via the k-nearest-neighbour distance elbow.

**Output:** integer cluster labels array `(n_benchmarks,)` + the chosen k/eps metadata.

---

## Stage 4 — Representative selection (`pipeline/select.py`)

**Input:** cluster labels + original feature matrix.

**Process:**
For each cluster, select the benchmark closest to the cluster centroid in the
*original* (pre-DR) normalised feature space (Euclidean distance).  This ensures the
selected benchmark is representative in the full-dimensional sense, not just in the
compressed view.

Tie-breaking: prefer benchmarks already in the `macro_base.yml` default-enabled list
(i.e. not commented out) over disabled ones.

**Output:** one benchmark name per cluster, with its cluster ID and distance-to-centroid.

---

## Stage 5 — Visualisation (`pipeline/visualize.py`)

Plots written to `results/<timestamp>/`:

| File | Content |
|---|---|
| `scatter_2d.png` | 2D embedding scatter, colour = cluster, label = benchmark name |
| `scree.png` | PCA: variance explained per component |
| `loading_heatmap.png` | PCA: feature loadings for the top components |
| `silhouette.png` | Silhouette score vs k (for k-sweep methods) |
| `feature_heatmap.png` | Raw normalised feature matrix, rows = benchmarks, colour = z-score |

All plots use matplotlib; no external JS/CSS.  Each function in `visualize.py` takes
the data object and a `Path` for the output file — easy to call selectively.

---

## Stage 6 — Report (`pipeline/report.py`)

**Output files** in `results/<timestamp>/`:

1. `representative_suite.yml` — a `benchmarks:` block ready to paste into a new
   experiment config:

   ```yaml
   # Generated by experiments/clustering — run YYYY-MM-DD
   # Technique: pca + kmeans(k=6)  Explained variance: 91.3%
   benchmarks:
     macro-menhir-monorepo:
       - menhir_ocamly
     macro-alt-ergo-monorepo:
       - alt_ergo_fill
     ...
   ```

2. `summary.md` — human-readable summary: benchmark count before/after, explained
   variance, cluster composition, and a coverage check against the `tags:` block in
   `macro_base.yml` (does the selected suite still exercise every runtime feature tag?).

3. `cluster_assignments.csv` — full table: benchmark, cluster_id, distance_to_centroid,
   is_representative (bool).

---

## Entry-point (`run_pipeline.py`)

```
python run_pipeline.py \
    --logs   gc-sweep-logs/turing-2026-06-24-Wed-160616 \
    --dr     pca \
    --cluster kmeans \
    --outdir results/$(date +%Y%m%d-%H%M%S)
```

Flags:
- `--logs`    path to a `gc-sweep-logs/<run>/` directory
- `--dr`      DR technique name (default: `pca`)
- `--cluster` clustering method (default: `kmeans`)
- `--k`       override cluster count (default: auto via silhouette sweep)
- `--outdir`  results output directory (default: `results/<timestamp>`)
- `--verbose` print per-stage progress to stdout *and* tee to `logs/<timestamp>.log`

Every run appends one line to `logs/run_history.jsonl`:
```json
{"timestamp": "...", "logs_dir": "...", "dr": "pca", "cluster": "kmeans",
 "k": 6, "benchmarks_in": 27, "benchmarks_out": 6, "explained_variance": 0.913}
```

---

## Extensibility checklist

| Change | What to do |
|---|---|
| New observability column in olly JSON | Nothing — `extract.py` picks it up automatically |
| New DR technique | Add a function to `TECHNIQUES` dict in `reduce.py` |
| New clustering method | Add a function to `CLUSTERERS` dict in `cluster.py` |
| New benchmark in `macro_base.yml` | Re-run the benchmark harness, re-run the pipeline |
| New perf group (grp2/grp3) | Run with that group; new `perf_*` columns appear automatically |

---

## Open questions / next steps

1. **Feature selection before DR:** Should we drop highly-correlated features (e.g.
   `total_heap_words` vs `minor_words` are likely ~0.99 correlated) before PCA to avoid
   double-counting?  Consider a pre-step that drops columns with |r| > 0.95 pairwise.

2. **Runtime-relative vs absolute features:** Wall time and allocation counts are
   confounded by benchmark workload size (coq takes 30 s, yojson takes 1 s).  Consider
   normalising each feature by `olly_wall_time_s` to get rates (e.g. allocations/second,
   GC pauses/second).  This would cluster benchmarks by *GC behaviour density* rather
   than raw magnitude.

3. **Cross-runtime stability:** Should we run the pipeline on a single runtime (e.g.
   `ocaml-trunk`) or aggregate across all runtimes?  Using a single reference runtime
   makes the clustering about the benchmark's inherent character, not the GC
   implementation.  Recommended: use `ocaml-trunk` as the reference.

4. **Tag coverage guard:** The report should warn if the selected subset fails to cover
   any runtime-feature tag from `macro_base.yml` that currently has active benchmarks
   (e.g. if no `weak_refs` benchmark is selected, the suite can't detect regressions in
   `Weak.Make`).

5. **Minimum viable run:** The pipeline can run today against the existing
   `gc-sweep-logs/turing-2026-06-24-Wed-160616/` logs — those files include olly sidecars
   for ~25 benchmarks across 5 runtimes, which is enough to prototype the clustering.
   Note the current logs use the `time_stats` modifier (not `perf_grpN`), so perf columns
   will be absent; only olly features will be available in the first run.
