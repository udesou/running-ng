# MMTk macro performance panel — `ocaml-mmtk@8544df0` vs stock 5.5.0-rc1

**Setup.** `ocaml-mmtk` = fplaunchpad/ocaml-mmtk `5.5+mmtk` @ `8544df0` (`ocaml-variants.5.5.0`); stock = OCaml `5.5.0-rc1` (`4090d6db`, the fork point). Native code, **dynamic heap** (MemBalancer; `MMTK_HEAP_SIZE_MB` unset), **best-of-3** wall time (min). Driven by [running-ng](https://github.com/udesou/running-ng) (`experiments/mmtk_macro.yml`), `/usr/bin/time` timing, ASLR off (`setarch -R`). Host: 32-core Linux x86-64.

> **Caveat — GC threads.** `MMTK_THREADS` was **not pinned**, so MMTk used its default *parallel* GC workers (e.g. menhir GenImmix ran at ~327% CPU). The upstream "quick panel" pins `MMTK_THREADS=1` for its sequential set, so these ratios are, if anything, favorable to MMTk. A matched `MMTK_THREADS=1` run is in progress.

## Wall-time ratio (mmtk / stock; lower = better) + max RSS (MB)

| benchmark | stock (s) | GenImmix × | Immix × | stock RSS | GenImmix RSS | Immix RSS |
|---|--:|--:|--:|--:|--:|--:|
| alt_ergo_fill | 5.12 | 7.80 ⚠️ | 1.28 | 928 | 1290 | 5490 |
| alt_ergo_yyll | 6.78 | 2.61 | 1.31 | 281 | 427 | 3519 |
| coqc_corelib_stress | 3.47 | 1.00 | 1.00 | 1540 | 1540 | 1541 |
| cpdf_blacktext | 2.52 | 1.33 | 1.10 | 242 | 414 | 1118 |
| cpdf_merge | 2.19 | 1.55 | 0.94 | 366 | 553 | 1226 |
| cpdf_scale | 12.78 | 1.08 | 0.94 | 482 | 843 | 2548 |
| cpdf_squeeze | 3.50 | 1.51 | 1.03 | 328 | 524 | 1182 |
| devkit_gzip | 2.53 | 1.02 | 1.02 | 14 | 51 | 564 |
| devkit_network | 5.07 | 1.30 | 1.27 | 76 | 168 | 648 |
| devkit_stre | 4.12 | 1.29 | 1.12 | 16 | 43 | 39 |
| eio_fiber_stream | 2.07 | 1.93 | 1.18 | 9 | 43 | 32 |
| frama_c_eva_sqlite | 7.06 | 0.96 | 1.56 | 446 | 444 | 5429 |
| frama_c_eva_t | 0.39 | 1.38 | 2.26 | 94 | 145 | 814 |
| goblint | 0.20 | 1.90 | 1.75 | 44 | 88 | 275 |
| irmin_mem_rw | 4.00 | 1.10 | 1.23 | 29 | 117 | 737 |
| jsoo | 3.80 | 1.49 | 1.03 | 263 | 589 | 1486 |
| liq_parse_typecheck | 15.76 | 1.85 | 1.25 | 15 | 77 | 72 |
| menhir_ocamly | 12.72 | 1.26 | 1.42 | 2692 | 2621 | 19779 |
| menhir_sql_parser | 1.24 | 1.60 | 1.35 | 322 | 490 | 1289 |
| menhir_sysver | 7.78 | 1.46 | 1.03 | 736 | 1015 | 2509 |
| ocamlc_self_compile | 5.21 | 1.84 | 1.55 | 1004 | 1101 | 7442 |
| ocamlformat_rocq | 1.86 | 1.73 | 1.47 | 272 | 527 | 1969 |
| owl_gc | 4.61 | **0.67** | 0.91 | 23 | 243 | 6765 |
| test_decompress | 1.72 | 1.62 | 1.22 | 10 | 64 | 53 |
| ydump_repeat | 3.49 | 1.02 | 0.96 | 6 | 36 | 31 |
| zarith_pi | 3.30 | 1.32 | 1.09 | 6 | 49 | 49 |

**Geomean wall ratio (n=26): GenImmix 1.47× · Immix 1.21×.**

- **GenImmix ≈ memory parity** with stock (RSS within a few %).
- **Immix is faster but RSS balloons** under the dynamic heap (menhir 20 GB vs 2.7; ocamlc 7.4 GB vs 1.0; owl 6.8 GB vs 0.02).
- **MMTk wins:** `owl_gc` GenImmix **0.67×** (RSS now 243 MB — vs the old fixed-heap 12 GB), `frama_c_eva_sqlite` 0.96×, `ydump_repeat`/`devkit_gzip` ≈1.0×.

## Not runnable under `8544df0` (excluded above)

| benchmark | symptom |
|---|---|
| devkit_htmlstream | hangs (multidomain / lwt) |
| sedlex_tokenize | hangs (multidomain) |
| liq_video_frames_pool | hangs (multidomain / ffmpeg) |
| pplacer_testsuite | SIGABRT `try_lock` — issue #11 cross-domain channel-finaliser residual |

## Notes

- **Fixed since the fork tip we previously tested:** the alt-ergo moving-GC SIGSEGVs are gone (all 3 run under the moving GenImmix/Immix plans — object pinning landed).
- `alt_ergo_unsat_smt2` is omitted from the table/geomean: its `--timelimit 15` makes wall a CPU-timer artifact.
- `alt_ergo_fill` GenImmix **7.8×** ⚠️ is a real outlier (GMP custom blocks vs the copy-nursery).
