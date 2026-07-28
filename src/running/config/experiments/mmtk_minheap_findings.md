# MMTk macro minheap — values + off-heap caveats

Measured 2026-06-24 with `python3 -m running minheap experiments/mmtk_minheap.yml`
(commit `14d1b8f`, OCaml `5.5+mmtk`, plans Immix / StickyImmix, `MMTK_HEAP_SIZE_MB`
binary search, `attempts=2`). Raw result: `~/mmtk_minheap_result.yml`.

`minheap` = smallest fixed MMTk heap (MB) a benchmark completes in. It measures the
**peak on-heap *live* set** managed by MMTk — NOT RSS, NOT total allocation, NOT
off-heap memory.

## How to read these values (IMPORTANT)

The binary search floor is **3** (`minheap.py` starts `lo=2`; `mid` never < 3). So a
reported **3 means "≤ 3 MB live, search bottomed out"**, not a measured boundary.

Three regimes were validated (RSS-vs-heap probe + synthetic micro-benches):

- **Footprint benches** (high minheap): live set is large; minheap is a real,
  trustworthy boundary. Use these for heap-relative sweeps.
- **On-heap-garbage benches** (minheap = floor): tiny live set, lots of collectable
  on-heap garbage. minheap honest but uninformative; RSS tracks the heap budget and
  never exceeds it (a smaller heap reclaims it).
- **Off-heap benches** (minheap = floor): real footprint is in **custom blocks**
  (bigarrays / GMP / zlib) that are `malloc`'d off-heap and do NOT count against
  `MMTK_HEAP_SIZE_MB`. minheap is **misleading** here — RSS climbs *past* the heap
  budget and grows with it (owl_gc: 128 MB @ 3 MB heap → 12 GB @ 16 GB heap).

### Why off-heap is unbounded under MMTk (confirmed by KC Sivaramakrishnan)

Off-heap stays off-heap; only the small custom-block proxy lives in the MMTk heap,
with the element data a plain `malloc` freed by the proxy's finalizer when collected.
Stock OCaml paces the major GC on off-heap/dependent memory
(`caml_alloc_dependent_memory` / the `mem,max` ratio in `caml_alloc_custom` via
`caml_adjust_gc_speed`) so finalizers release it promptly. Under MMTk that bookkeeping
still runs but is **inert**: it bottoms out in `caml_request_major_slice`, and the
stock major slice is a no-op since MMTk owns collection — so off-heap pressure never
nudges an MMTk GC. MMTk paces on its own heap occupancy, which those bytes don't
contribute to. Upstream fingerprint: `lib-bigarray/subarraystub` testsuite failure.
Feeding off-heap/dependent pressure into MMTk's trigger is on KC's TODO.

## Classification

| benchmark            | Immix | Sticky | class            | note                                  |
|----------------------|------:|-------:|------------------|---------------------------------------|
| menhir_ocamly        |  6059 |   3782 | footprint        | trustworthy                           |
| ocamlc_self_compile  |  2233 |   1346 | footprint        | trustworthy                           |
| sedlex_tokenize      |  2010 |   1002 | footprint        | trustworthy                           |
| frama_c_eva_sqlite   |   673 |    516 | footprint        | trustworthy                           |
| menhir_sysver        |   693 |    610 | footprint        | trustworthy                           |
| cpdf_merge           |   620 |    390 | footprint        | trustworthy                           |
| cpdf_squeeze         |   508 |    292 | footprint        | trustworthy                           |
| cpdf_scale           |   442 |    446 | footprint        | trustworthy                           |
| cpdf_blacktext       |   360 |    290 | footprint        | trustworthy                           |
| menhir_sql_parser    |   358 |    218 | footprint        | trustworthy                           |
| devkit_htmlstream    |   340 |    275 | footprint        | trustworthy                           |
| jsoo                 |   313 |    278 | footprint        | trustworthy                           |
| ocamlformat_rocq     |   236 |    121 | footprint        | trustworthy                           |
| liq_video_frames_pool|   101 |    102 | footprint        | trustworthy                           |
| devkit_network       |    64 |     63 | footprint        | trustworthy                           |
| frama_c_eva_t        |    17 |     15 | footprint(small) | low but real                          |
| irmin_mem_rw         |    16 |     17 | footprint(small) | low but real                          |
| goblint              |    11 |     11 | footprint(small) | low but real                          |
| owl_gc               |     3 |      3 | **OFF-HEAP**     | BLAS bigarrays; RSS→12 GB @ 16 GB heap |
| zarith_pi            |     3 |      3 | **OFF-HEAP**     | GMP limbs; RSS 5.5 GB @ 4 GB heap     |
| coqc_corelib_stress  |     3 |      3 | static-mmap      | coqlib mmap ~1.5 GB; live heap ≤1 MiB |
| test_decompress      |     3 |      3 | on-heap-garbage  | floor; honest                         |
| eio_fiber_stream     |     3 |      3 | on-heap-garbage  | floor; honest                         |
| liq_parse_typecheck  |     3 |      3 | on-heap-garbage  | floor; honest                         |
| ydump_repeat         |     3 |      3 | on-heap-garbage  | floor; honest                         |
| devkit_stre          |     3 |      3 | on-heap-garbage  | floor; honest                         |
| devkit_gzip          |     4 |      5 | on-heap-garbage  | just above floor                      |

EXCLUDED (crash under MMTk, minheap meaningless): alt_ergo_fill / yyll / unsat_smt2
(SIGSEGV, moving-GC custom-block bug), pplacer_testsuite (SIGABRT, channel finalizer).

## GC-count cross-check (independent confirmation)

Two independent signals confirm the classification beyond the RSS-vs-heap probe.

**Stock side — bytes allocated per minor GC** (olly, ocaml-5.4.1, s=262144 ⇒ 2 MB
minor heap; `total_heap_words*8 / minor`). On-heap benches collect once per ~2 MB
(= minor heap filling); off-heap benches collect far more often than on-heap
allocation explains — that excess is stock's off-heap/dependent-memory pacing:

| bench               | stock minor | on-heap total | bytes/GC | driven by |
|---------------------|------------:|--------------:|---------:|-----------|
| coqc_corelib_stress*|        6562 |       13.7 GB |   2.1 MB | on-heap   |
| test_decompress     |        2223 |        4.7 GB |   2.1 MB | on-heap   |
| eio_fiber_stream    |        5232 |        9.3 GB |   1.8 MB | on-heap   |
| liq_parse_typecheck |       27819 |         53 GB |   1.9 MB | on-heap   |
| ydump_repeat        |        1536 |        2.1 GB |   1.4 MB | on-heap   |
| devkit_stre         |        7729 |       13.9 GB |   1.8 MB | on-heap   |
| devkit_gzip         |        1935 |        2.8 GB |   1.5 MB | on-heap   |
| owl_gc              |       62970 |       0.56 GB | **8.9 KB** | **off-heap** |
| zarith_pi           |       78697 |         32 GB | **406 KB** | **off-heap** |

**MMTk side — GC count at the 3 MB minheap** (MMTK_VERBOSE). On-heap benches do
thousands of GCs, same order as stock's minor count (small live set + GB of churn).
off-heap benches do far fewer than stock (off-heap pressure doesn't trigger MMTk):

| bench               | MMTk GC @3MB | stock minor | ratio | note |
|---------------------|-------------:|------------:|------:|------|
| liq_parse_typecheck |       25001  |       27819 |  0.90 | on-heap ✓ |
| devkit_stre         |        5818  |        7729 |  0.75 | on-heap ✓ |
| eio_fiber_stream    |        3429  |        5232 |  0.66 | on-heap ✓ |
| test_decompress     |        2975  |        2223 |  1.34 | on-heap ✓ |
| ydump_repeat        |         999  |        1536 |  0.65 | on-heap ✓ |
| devkit_gzip         |   434 @8MB   |        1935 |   —   | on-heap ✓ (scales 1/heap: 51 @64MB) |
| zarith_pi           |       12012  |       78697 |  0.15 | mixed: on-heap churn + off-heap GMP |
| owl_gc              |         414  |       62970 | 0.007 | **off-heap** (150x fewer) |

*coq: no MMTk verbose report (Rocq exits via _exit, bypassing the atexit hook).
Proxy = wall time is **flat 3.46–3.48s from 3 MB to 16384 MB heap** ⇒ negligible
GC activity ⇒ low on-heap churn (footprint is the coqlib mmap, not GC heap). The
stock 6562/13.7 GB is the older 5.4.1 `coqc`, a different binary than the Rocq
`coqc_bin.exe` measured under MMTk — not directly comparable.

## Recommendation

- Heap-relative sweeps: use only the **footprint** rows.
- **off-heap** rows: sweep at absolute heap points and report RSS — minheap does not
  characterize them; they will grow off-heap memory ∝ heap until the pacing gap is fixed.
- **on-heap-garbage / static-mmap** rows: minheap is honest (small live set) but near
  the floor; not useful as a sweep base.
