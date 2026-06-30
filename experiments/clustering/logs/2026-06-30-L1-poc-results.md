# Step log: PoC clustering runs (2026-06-30)

## Runs performed

Two end-to-end passes of the pipeline were completed today.

### Pass 1 — reused logs (`data/turing-2026-06-23-Tue-221257`)
Copied from `gc-sweep-logs/` to avoid a fresh benchmark run during prototyping.
Single invocation per benchmark (the original run had 1 invocation).
Results in `results/poc/`.

### Pass 2 — fresh run (`logs/turing-2026-06-30-Tue-122324`)
Driven by the new self-contained config `clustering_run.yml` (macro_base.yml
inlined, single runtime `ocaml-5.4.1`, `perf_grp1|re_par-22|md_par-8|pin_lavyek`).
3 invocations per benchmark. All 31 benchmarks built from cached binaries; all
31 completed with both olly and perf sidecars (95 files total).
Results in `results/poc-fresh/`.

---

## Feature matrix

Both passes produced a **30 × 20** feature matrix after dropping:
- 1 benchmark with incomplete data (coqc_corelib_stress — olly sidecar present
  but a derived feature (major_per_minor) is NaN after median aggregation,
  likely because it had zero minor collections in the olly trace).
- `compactions` column dropped (zero variance — no benchmark triggered a
  compaction at default GC settings under 5.4.1).

Features used (20):

| Group | Features |
|---|---|
| Timing | wall_time_s, cpu_time_s, gc_time_s, gc_overhead_pct |
| Memory | max_rss_mb |
| GC pauses | mean_latency_ms, p95_latency_ms, p99_latency_ms, max_latency_ms |
| Allocation | total_heap_Mw, minor_heap_Mw, promoted_pct |
| Collections | minor_colls, major_colls, major_per_minor |
| perf (hardware) | perf_task-clock, perf_page-faults, perf_cycles, perf_instructions, perf_instructions_insn_per_cycle |

---

## PCA

6 principal components explain ≥90% variance across 20 features — confirming
the feature space is highly compressible (many timing and allocation metrics are
correlated). Clustering was performed in this 6-D decorrelated PCA space rather
than the original 20-D space to avoid k-means being dominated by redundant
correlated dimensions.

---

## k-means results

Silhouette sweep over k = 4…10; best k = **5** in both passes (silhouette ≈ 0.35).

| Cluster | Size | Representative (pass 1) | Representative (pass 2) | Character |
|---|---|---|---|---|
| 1 | 10–11 | `irmin_mem_rw` | `eio_fiber_stream` | Moderate GC, low alloc, short wall time |
| 2 | 7 | `menhir_sysver` | `menhir_sysver` | Higher alloc, longer wall time |
| 3 | 8 | `cpdf_blacktext` | `cpdf_blacktext` | Fast, low-GC text/compiler tools |
| 4 | 2 | `owl_gc` | `owl_gc` | FFI/Bigarray outlier (OpenBLAS) |
| 5 | 2 | `liq_video_frames_pool` | `liq_video_frames_pool` | Off-heap custom-block outlier |

Clusters 2–5 are identical across both passes. The only change is the
representative for cluster 1 shifting from `irmin_mem_rw` to `eio_fiber_stream`
— both are members of the same 11-benchmark cluster; the centroid moved slightly
when medians were computed over 3 invocations instead of 1.

---

## Representative suite (pass 2, canonical)

```yaml
benchmarks:
  macro-cpdf-monorepo:
    - cpdf_blacktext
  macro-eio:
    - eio_fiber_stream
  macro-liq-video-frames:
    - liq_video_frames_pool
  macro-menhir-monorepo:
    - menhir_sysver
  macro-owl:
    - owl_gc
```

5 benchmarks covering all 5 behavioral clusters found in 30 active benchmarks.

---

## Observations and next steps

1. **Cluster 1 representative is unstable** between 1 and 3 invocations.
   `irmin_mem_rw` and `eio_fiber_stream` have very similar feature vectors and
   sit close to the centroid of a large (11-member) cluster. More invocations
   or bootstrapped selection (pick the most frequently elected representative
   across resampled subsets) would stabilise this.

2. **`coqc_corelib_stress` is consistently dropped** due to a NaN in
   `major_per_minor`. Investigate whether this is a zero-minor-collections run
   or an olly sidecar issue. If the former, impute 0 rather than NaN.

3. **`compactions` is always zero** at default GC settings — this feature will
   only become informative when runs include a compactor-enabling config
   (e.g. the `auto_compact_comparison` experiment). Consider gating its
   inclusion on non-zero variance rather than always dropping it.

4. **Cluster 4 (`owl_gc` + `zarith_pi`) and cluster 5
   (`liq_video_frames_pool` + `pplacer_testsuite`) each have only 2 members.**
   These are genuine outliers (FFI-heavy and off-heap-accounting-heavy
   respectively) and their isolation is correct. However with only 2 members
   the centroid is not stable — adding more benchmarks in these categories
   would strengthen the clusters.

5. **Tag coverage check (not yet automated):** the 5 selected representatives
   cover `eio_fibers`/`effects` (eio_fiber_stream), `ffi_bulk`/`bigarrays`
   (owl_gc), `off_heap_accounting` (liq_video_frames_pool), and general
   allocation-heavy work (menhir_sysver, cpdf_blacktext). Currently uncovered
   by the representative set: `weak_refs` (alt-ergo), `lwt` (irmin),
   `marshal` (ocamlc_self_compile). Automating this coverage check against
   `macro_base.yml` tags is the next priority.
