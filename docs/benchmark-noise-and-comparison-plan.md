# Benchmark noise, comparison framework, and benchmark calibration

Three independent issues surfaced while reviewing the 2026-04-21 macro-bench
output in the notebooks. They are tracked together because their fixes
inform each other (in particular, calibration claims are only credible
once noise is reduced). Each can be worked on separately.

## Context

- Run: `gc-sweep-logs/obelisk-2026-04-21-Tue-103805/` (208 cells × 3
  invocations = 624 measurements).
- Config: `src/running/config/fp_flambda_macro_5.4.1_vs_d8bb46c.yml`
  (8 runtimes: 2 versions × {baseline, fp, flambda, fp-flambda}).
- Visualisation: `notebooks/A_regression_dashboard.ipynb` and
  `notebooks/B_runtime_behaviour.ipynb`.

What we noticed:

1. The current "everything vs one global baseline" comparison conflates
   *version effect* with *flag effect*. A 5.5/flambda vs 5.4.1/baseline
   ratio is not a useful regression signal because it bundles two
   independent changes.
2. Some benchmarks behave outside the useful operating envelope —
   `coqc_corelib_stress` appears to spend ~all wall time in GC; other
   benchmarks trigger no major collections at all.
3. Running rocq solo took ~44s; the same benchmark inside the full
   suite ran for ~10× longer. The machine is not isolated and the
   benchmark-to-benchmark state leaks between runs.

The existing 2026-04-21 dataset is preserved and used as the "untuned
obelisk" baseline against which any of the fixes below will be measured.

---

## Issue 1 — Paired comparisons via YAML

### Problem

The runbms YAML lists 8 runtimes flat. There is no schema for "which
pairs of runtimes are intended to be compared". The notebooks therefore
default to ratios against a single baseline, which is inadequate for any
2D matrix (compiler version × flag combo, or any other parameterised
sweep with more than one independent axis).

### Solution

Add a `comparisons:` section to the runbms config YAML, alongside
`runtimes:` and `configs:`:

```yaml
comparisons:
  - name: "version-effect-baseline"
    a: "ocaml-5.4.1"
    b: "ocaml-d8bb46c"
  - name: "version-effect-fp"
    a: "ocaml-5.4.1-fp"
    b: "ocaml-d8bb46c-fp"
  - name: "version-effect-flambda"
    a: "ocaml-5.4.1-flambda"
    b: "ocaml-d8bb46c-flambda"
  - name: "version-effect-fp-flambda"
    a: "ocaml-5.4.1-fp-flambda"
    b: "ocaml-d8bb46c-fp-flambda"
  - name: "flambda-effect-on-5.4.1"
    a: "ocaml-5.4.1"
    b: "ocaml-5.4.1-flambda"
  - name: "fp-effect-on-5.4.1"
    a: "ocaml-5.4.1"
    b: "ocaml-5.4.1-fp"
  - name: "flambda-effect-on-d8bb46c"
    a: "ocaml-d8bb46c"
    b: "ocaml-d8bb46c-flambda"
  # ... etc
```

`a` and `b` are runtime names from the `runtimes:` block. `name` is a
short label used to group the views in the notebook.

When `comparisons:` is absent, the notebook falls back to "every
non-baseline variant vs the notebook's `BASELINE` variable" — same as
today. This default is documented at the top of Notebook A, alongside a
note that for any serious 2D matrix the YAML schema should be used.

### Pipeline

For the notebook to consume comparisons declared in the YAML, the
config has to reach the log directory. running-ng should materialise the
post-`includes:` / post-`overrides:` resolved config into a single file
inside each run's log dir (e.g. `<log-dir>/meta.yml`). The notebook
loads that file and reads `comparisons:` from it.

### Implementation steps

1. **Schema.** Decide whether `name` is required or optional. Prefer
   required, because the notebook uses it as a section heading. If absent,
   default to `f"{a}_vs_{b}"`.
2. **running-ng change.** When `runbms` starts, write the resolved
   config (post-include, post-override) to `<log-dir>/meta.yml`. This is
   useful beyond comparisons — it makes runs self-describing without the
   notebook needing to re-read the source YAML.
3. **Loader change.** `macrobench_loader` gains
   `load_comparisons(logs_dir) -> list[Comparison]` that reads
   `meta.yml` (returning `[]` if absent) and resolves `a`/`b` to the
   `variant` strings used in the DataFrame
   (`"<version>/<flags>"`).
4. **Notebook A change.** New section "Paired comparisons" that
   iterates the comparison list and renders, per pair: ratio table
   (top-N regressions, top-N improvements), tornado plot, time × memory
   tradeoff scatter restricted to that pair. Falls back to the global
   baseline when no comparisons declared.

### Until the YAML support lands

A top-of-notebook list (`COMPARISONS = [(a, b), …]`) is the temporary
escape hatch. Same notebook code, the only difference is where the list
comes from. This makes Issue 1 partially resolvable today without
waiting for running-ng changes.

### Estimate

- Stage 1 (notebook list): half a day.
- Stage 2 (YAML schema + materialise + loader): one day, mostly in
  running-ng.

---

## Issue 2 — Benchmark calibration

### Problem

Some benchmarks fall outside any useful operating envelope:

- `coqc_corelib_stress` appears to spend close to 100% of wall time in
  GC. Either the input is wildly out of scale for the configured GC
  parameters, or the workload genuinely is GC-bound and we should know
  that explicitly.
- Some benchmarks trigger zero major collections — the workload is
  too small or too short to exercise the major GC, so the run
  effectively measures only minor GC behaviour. That's fine if it's
  intentional, but it should be visible.
- Some benchmarks have wall times so short that 5–10ms of OS scheduling
  noise dominates the signal.

### Target operating envelope

For a benchmark to produce a useful runtime/GC signal it should sit
roughly in:

- **Wall time:** 10–20 s. Long enough that fixed-cost startup is
  negligible, short enough that an iteration loop is bearable.
- **GC overhead:** 5–30% of wall time. Below 5% and the benchmark
  doesn't exercise the GC much; above 50% something is broken.
- **Major collections:** ≥ 10 over the run. If zero or very small the
  major GC is not being meaningfully tested.
- **Allocation rate:** above ~10 MB/s sustained. Below that the program
  is mostly compute and the GC view is mostly noise.

These are starting numbers, not hard rules. Some benchmarks are
deliberately mostly-mutator (e.g. `zarith_pi`) and that's fine — the
envelope flags candidates for review, it doesn't reject them.

### What goes in the notebooks

Notebook B gains a "Health check" section that surfaces, per benchmark
× variant: wall time, GC overhead, major collection count, allocation
rate, and a column flagging which envelope rules were violated. Sorted
worst-first. The output is a table; no plot needed.

This is a pure visualization change — it does not fix any benchmark, it
just tells the bench owner which to look at.

### What goes in `~/benches`

The actual fix for an out-of-envelope benchmark lives in that
benchmark's input args / data file in `~/benches`. Each fix is its own
small project owned by whoever maintains that benchmark. We track them
as a list in `~/benches/CALIBRATION_TODO.md` (or similar) seeded from
the notebook's flagged candidates. Top-of-list candidates from this
run:

- [ ] `coqc_corelib_stress` — GC-overhead investigation (likely needs
      smaller corpus or different OCAMLRUNPARAM).
- [ ] Any benchmark that completes in < 0.5 s — too short to be useful.
- [ ] Any benchmark with zero major collections — confirm whether
      that's intentional and tag accordingly, or extend the workload.

### A/B methodology using existing data

Once Issue 3 is fixed, re-run the same YAML on tuned obelisk. The
2026-04-21 data is the "before" snapshot; the new run is the "after".
Comparing health-check tables between the two snapshots tells us:

- For benchmarks that stayed flagged: input tuning is genuinely
  required (it wasn't just machine noise).
- For benchmarks that became unflagged: the original flag was a false
  positive caused by noise; no input change needed.

This is cheap to do because we already have the "before".

### Estimate

- Notebook B health view: half a day.
- Per-benchmark calibration: variable, hours to days each, owned by
  individual bench authors.

---

## Issue 3 — Reduce machine noise

### Problem

`rocq` ran ~44 s solo and ~440 s inside the full suite — a 10× bias
that has nothing to do with rocq itself. The cause is a combination of:

- CPU frequency scaling and/or thermal throttling as the machine warms up.
- Page cache state accumulating across benchmarks.
- Background processes (`tuned`, `snapd`, indexers, cron) competing
  for cores.
- IRQs and kernel work on the same cores as the benchmark.
- Possibly NUMA mis-pinning if obelisk has multiple memory nodes.

The proposal already plans to fix this on a future dedicated runner
(§System Architecture: "CPU frequency is pinned, turbo boost is
disabled, specific cores are isolated for benchmark execution via
`isolcpus`"). The current obelisk machine is explicitly described as a
test machine with limited tuning. The plan below is what to do
incrementally on obelisk while the dedicated-runner work is in flight.

### Stage A — Cheap one-liners (do today, ~10 min)

Validate-then-apply, in order. Verify each step's effect with the
obvious tool before moving on.

1. **Disable and mask `tuned`** (per Edwin's guidance — it has dynamic
   nudging that keeps coming back if only stopped):
   ```bash
   sudo systemctl mask --now tuned
   ```
2. **Disable turbo boost.** CPU-vendor-dependent.
   - Intel `intel_pstate`:
     ```bash
     echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo
     ```
   - AMD `amd_pstate` (Zen 2+): different path; check
     `cpupower frequency-info` first.
   Verify after with `cpupower frequency-info` — actual frequency
   should match nominal, not turbo, even under load.
3. **Lock the governor** to `performance` (no scaling):
   ```bash
   sudo cpupower frequency-set -g performance
   ```
4. **Disable ASLR** for reproducibility:
   ```bash
   echo 0 | sudo tee /proc/sys/kernel/randomize_va_space
   ```
5. **Audit running services and disable known noise sources**:
   ```bash
   systemctl --type=service --state=running
   ```
   Common offenders: `snapd`, `cron` / `anacron`, `unattended-upgrades`,
   `man-db.timer`, `apt-daily.timer`, `tracker`, `packagekit`. Disable
   per-service for the duration of benchmark runs.

### Stage B — Per-benchmark isolation (the real fix for the rocq case)

Most of rocq's 10× slowdown comes from cumulative state across
benchmarks. Fix:

6. **Drop the page cache between benchmarks**:
   ```bash
   sync && echo 3 | sudo tee /proc/sys/vm/drop_caches
   ```
   Add this as a hook in runbms's per-benchmark loop. Cold-cache state
   is also closer to first-run user experience, which is more
   representative of real-world behaviour anyway.
7. **Sleep between benchmarks** for thermal stabilization:
   ```bash
   sleep 10
   ```
   Rough rule: 5–15 s. Verify that CPU temperature returns to within
   3°C of idle before the next benchmark starts; bump the sleep up if
   not. `sensors` or `cat /sys/class/thermal/thermal_zone*/temp` works.
8. **`taskset` the benchmark to specific cores**:
   ```bash
   taskset -c 4-7 <benchmark>
   ```
   Pick a contiguous range of cores away from CPU 0 and 1 (legacy IRQs
   land on CPU 0). Honest caveat: `taskset` only constrains *the
   benchmark*; other processes are still allowed on those cores by the
   scheduler.

### Stage C — Real CPU isolation (when obelisk gets a kernel cmdline edit)

`taskset` alone is partial. For real isolation:

9. Add to the kernel boot line (e.g. via grub / systemd-boot):
   ```
   isolcpus=4-7 nohz_full=4-7 rcu_nocbs=4-7
   ```
10. Move IRQ affinity off cores 4–7:
    ```bash
    for irq in $(grep -E '^[ ]*[0-9]+:' /proc/interrupts | cut -d: -f1); do
      echo 0-3 | sudo tee /proc/irq/$irq/smp_affinity_list
    done
    ```
    (Some IRQs refuse to be moved — that's expected.)
11. Then `taskset -c 4-7` actually sticks.

This is more invasive (requires reboot) and should land on the
dedicated runner, not necessarily on obelisk. But if obelisk is a
research workhorse this is what to aim for.

### What about random benchmark order?

We considered randomising benchmark order to convert systematic bias
(rocq always last → always slow) into random variance. **Not the right
primary fix.** Random order doesn't eliminate the noise — it just
redistributes it. With Stage B in place (drop caches + sleep +
frequency lock) the per-benchmark starting state is uniform regardless
of order, so order doesn't bias anything; alphabetical is fine and
produces *less* total variance because every run is identical.

Random order is useful as a sanity check: run the same suite alphabetical
and randomised, compare. Significant differences imply hidden state that
Stage B did not address. Treat random order as a debugging tool, not a
default.

### Verification

We have an "untuned obelisk" baseline already
(`gc-sweep-logs/obelisk-2026-04-21-Tue-103805/`). Re-run the same YAML
after Stages A+B (no kernel changes needed) and compare:

- For each benchmark, the median wall time should be within ±5% of the
  solo run on tuned obelisk. If rocq still goes 44 → 400 s, the
  per-benchmark isolation didn't take.
- The IQR per (benchmark, variant) cell should shrink — same machine,
  same workload, less variance.
- The cross-benchmark wall-time ranking should stabilise (run the
  alphabetical → randomised sanity check from the previous section).

A sustained < ±2% IQR on a benchmark that previously had 10× drift is
strong evidence the noise has been fixed. Failing that, we have a
hidden cause (NUMA, hardware issue, hyperthreading) and the proposal's
dedicated-runner timeline becomes more urgent.

### Estimate

- Stage A: 10 minutes.
- Stage B: half a day (running-ng plumbing for the per-benchmark
  hooks).
- Stage C: depends on whether obelisk is allowed a reboot + kernel
  cmdline change. Hours of work, but the scheduling is the bottleneck.

---

## Recommended order

1. **Issue 1 stage 1** (top-of-notebook `COMPARISONS` list). Half a
   day. Makes every subsequent comparison legible.
2. **Issue 3 stages A + B** (one-liners and per-benchmark isolation).
   Half a day. Without this, none of the other measurements are
   trustworthy.
3. **Re-run the same YAML on tuned obelisk** for the "after" snapshot.
   Hours, mostly waiting.
4. **Issue 2 health view** in Notebook B. Half a day. Now the flagged
   benchmarks are credibly bench-content issues, not machine noise.
5. **Issue 1 stage 2** (`comparisons:` schema + running-ng `meta.yml`
   materialisation + loader change). One day.
6. **Issue 2 calibration** of individual benchmarks in `~/benches`.
   Per-benchmark, owned by bench authors.
7. **Issue 3 stage C** (kernel cmdline isolation). Schedule
   permitting; otherwise carry as future work for the dedicated runner.

---

## Out of scope

The following are real concerns but belong elsewhere:

- Statistical layer (Wilcoxon, Cliff's Delta, hierarchical speedup at
  95%) — proposal Phase 3, requires N ≥ 30 invocations.
- CI integration and PR bot — proposal Phase 4.
- Persistent result store and dashboard — proposal Phase 5.
- The dedicated-runner machine setup beyond what's needed to validate
  obelisk improvements — proposal §System Architecture.
- USDT / GC traces — separate work, see runtime_events_tools and
  ocaml_usdt.
