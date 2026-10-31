# Local traffic-engineering experiment plan

## Purpose and evidence boundary

This plan uses the existing scripts and their existing arguments. It does not
start any experiment. Commands are PowerShell commands run from the project
directory:

```powershell
Set-Location 'C:\Users\Pratham Soneja\Documents\BITS Sem 1\ADV CN\CN Project\cn_project_test\cn_project_test\Soroush'
$Python = '.\.venv\Scripts\python.exe'
```

Run the command blocks in the same PowerShell window so `$Python`, `$Stamp`,
and the output-path variables remain set. If you open a new window, repeat the
`Set-Location` and variable assignments before continuing.

### What is already in the repository

- `extension\analysis_uninett_1\median_summary.csv` has 448 rows, covering 64
  Uninett cases, 7 controllers, and 15 epochs per case. That folder has no raw
  epoch CSV or run configuration, so its sampling, seed, and runtime procedure
  cannot be fully audited.
- `extension\uninett_runs\uninett_full` is **not complete**: its configuration
  requests the large 16-path grid, but its per-epoch CSV currently has only 9
  rows (one epoch for one case). Do not use it as a completed result or resume
  it with the settings below.
- `extension\.path-cache` already contains `Uninett2010_k3.pkl` and
  `Uninett2010_k16.pkl`; the 3-path screening and comparison smoke commands
  should reuse the k=3 cache.
- `progress_review_2\runs\20261010_185812` is a completed static PR2 run:
  `include_eb` is true, `repeats` is 3, the raw CSV has 1,296 rows, and its
  median summary has 432 rows. It includes SWAN and Equidepth Binner.

The PR2 run is a static traffic-matrix study. The extension sweep is a dynamic
multi-epoch study. Their inputs, scoring references, and objectives differ;
report them in separate tables/figures. Current dynamic scripts compare
Hybrid Basic, Hybrid Fixed, Hybrid Adaptive, Hybrid ML Elephant/Mice,
periodic GB, forecast GB (`gb_only`), AW, and repository B4/B4+. They do not
run SWAN or Equidepth Binner on the dynamic workload. Do not claim a single
matched comparison across all requested algorithms from these separate runs.

## Budget and stage order

Planning estimates use the existing 3-path smoke result (about 12–15 seconds
of summed serial controller time for one epoch across nine methods) and are
not guarantees. Solver, host load, and workload differences can increase them.
The main screening design is 20 cases × 15 epochs × 9 methods: 2,700
method-epoch rows. One separate runtime repeat approximately doubles the
allocation work.

| Work | Estimated active time |
|---|---:|
| Stage 0 environment and two smoke tests | 10–30 minutes |
| Stage 2 instrumentation implementation and smoke | 30–60 minutes, plus under 10 minutes for the smoke |
| Stage 1 main screening, one separate serial runtime repeat | 2–4 hours |
| Stage 3 component profiling | 30–60 minutes, including temporary timing instrumentation |
| Stage 4 elephant/mice selection and held-out check | 20–45 minutes |
| Optional Stage 5 confirmation | 2–4 hours |

Expected total without Stage 5: about 3.5–7.5 hours. With Stage 5: about
5.5–11.5 hours at the upper planning estimate. Do not start Stage 5 unless the
actual elapsed time for Stages 0–4 is no more than 7.5 hours and the remaining
estimate for Stage 5 is no more than 4 hours. Stop adding work at 11 hours to
leave a one-hour buffer. These are planning estimates, not a runtime
guarantee; there is no repository flag that hard-limits wall-clock runtime.
The resumable sweep saves completed epochs; interrupt it with `Ctrl+C` and
resume with the same command, output directory, and settings. If any stage
exceeds its estimate, skip Stage 5 rather than risking the cap.

**Dependency gate:** Stage 2 instrumentation is now implemented. Pass its
smoke test **before the Stage 1 production run** so the new metrics and
comparable churn are verified before the long run.

## Stage 0 — Environment and smoke validation

### 0.1 Verify the selected Python, packages, solver license, and cache

**Working directory:** project directory shown above.

```powershell
Test-Path $Python
& $Python --version
& $Python -m pip check
& $Python -c "import gurobipy, matplotlib, networkx, numpy, pandas; print('gurobipy', gurobipy.gurobi.version()); print('numpy', numpy.__version__); print('pandas', pandas.__version__); print('networkx', networkx.__version__); print('matplotlib', matplotlib.__version__)"
& $Python -c "import gurobipy as gp; m=gp.Model(); m.Params.OutputFlag=0; x=m.addVar(lb=0.0); m.setObjective(x,gp.GRB.MAXIMIZE); m.addConstr(x<=1.0); m.optimize(); print('status',m.Status,'objective',m.ObjVal); assert m.Status==gp.GRB.OPTIMAL and abs(m.ObjVal-1.0)<1e-8"
Get-ChildItem -Force 'extension\.path-cache' | Select-Object Name,Length
```

**Estimated time:** 1–3 minutes. The tiny LP is a license/solver check, not a
traffic-engineering experiment.

**Expected outputs:** Python and package versions; `pip check` reports no
broken requirements; the LP reports optimal status and objective 1; the cache
listing includes `Uninett2010_k3.pkl`. No experiment output files are created.

**Inspect before continuing:** if Python is missing, imports fail, `pip check`
reports a problem, the LP cannot obtain a license, or the k=3 cache is absent,
stop and resolve that issue first. Do not reinstall the environment unless a
check actually fails.

### 0.2 Smoke the resumable sweep

Use a fresh output directory. Set `$Stamp` once for both smoke outputs:

```powershell
$Stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$SweepSmoke = "extension\uninett_runs\stage0_sweep_$Stamp"
& $Python extension\run_uninett_sweep.py --traffic-models uniform --scales 32 --scenarios normal --matrix-selection first --epochs 1 --num-paths 3 --path-processors 1 --controller-workers 1 --runtime-repeats 0 --max-cases 1 --output $SweepSmoke
$SmokeRows = Import-Csv "$SweepSmoke\per_epoch.csv"
$SmokeRows.Count
$SmokeRows | Group-Object method | Select-Object Name,Count
Get-ChildItem $SweepSmoke | Select-Object Name,Length
```

**Estimated time:** about 1–3 minutes, given the existing k=3 cache.

**Expected files:** `per_epoch.csv`, `runtime_benchmarks.csv`, `summary.csv`,
`median_summary.csv`, `run_config.json`, `checkpoint.pkl`, and the six report
plots. Expected counts: 9 `per_epoch.csv` rows, 9 runtime rows, 9 summary rows,
and 9 median-summary rows—one epoch for each of the 7 extension controllers
plus B4 and B4+.

**Inspect before continuing:** confirm nine distinct methods, one case, one
epoch, zero capacity violations, `controller_workers` 1, `num_paths` 3, and
the requested normal scenario in `run_config.json`. The capacity and demand
metrics must be finite. This smoke checks the pipeline; it is not evidence
about controller quality.

### 0.3 Smoke the matched single-case B4/B4+ comparator

```powershell
$CompareSmoke = "extension\comparison_runs\stage0_compare_$Stamp"
& $Python extension\compare_hybrid_baselines.py --topology Uninett2010.graphml --traffic-model uniform --scale 8 --scenario sudden_spike --seed 0 --epochs 9 --num-paths 3 --path-processors 1 --controller-workers 1 --runtime-repeats 1 --output $CompareSmoke
$CompareRows = Import-Csv "$CompareSmoke\per_epoch.csv"
$CompareRows.Count
$CompareRows | Group-Object method | Select-Object Name,Count
Get-ChildItem $CompareSmoke | Select-Object Name,Length
```

**Estimated time:** about 3–10 minutes. B4/B4+ and the quality plus separate
serial timing pass make this slower than the one-epoch smoke.

**Expected files:** `per_epoch.csv`, `summary.csv`, and
`comparison_config.json`. Expected counts for the current source are 81
per-epoch rows (9 methods × 9 epochs) and 9 summary rows. Older saved smoke
folders may have six methods because they were produced by an earlier
revision; use counts from this fresh run.

**Inspect before continuing:** confirm each of the nine methods has 9 rows,
the config says 3 paths, serial workers, 9 epochs, and one runtime repeat,
and capacity violations are zero. Confirm B4 and B4+ complete without an
exception. Warnings must be reviewed rather than mistaken for a successful
metric if the corresponding values are non-finite.

### 0.4 PR2 check — no new solver smoke is needed

The saved PR2 run already has three repeats, SWAN, and EB; do not spend time
rerunning it for this plan. If you need to verify which matrices the current
script resolves without solving them, this is a plan-only command:

```powershell
& $Python progress_review_2\run_pr2_full_evaluation.py --topologies Uninett2010.graphml --traffic-models uniform --scales 8 --repeats 1 --max-scenarios 1 --include-eb --plan
```

**Estimated time:** under 1 minute; no output directory or solver allocation
is created.

**Inspect before continuing:** confirm one Uninett matrix is resolved. For
the existing PR2 results, verify the saved config has `include_eb: true`,
`repeats: 3`, and the raw/summary counts listed above. PR2 has no
`--controller-workers` option; do not add one.

**Stage 0 decision:** proceed only if both extension smoke tests complete and
the solver license works. Do not rerun the full PR2 suite; its completed
results are available already.

## Stage 2 — Add independent fairness and churn metrics

### Instrumentation status

The current dynamic outputs already include aggregate served demand, unmet
demand, unmet fraction, `fairness_vs_periodic_gb`, runtime, and capacity
violations. Controller state also computes `allocation_changes` and
`gb_reruns` in `hybrid_hybrid_te.py`, but the matched comparison output
previously dropped those fields. The implementation now records per-OD
offered demand and served rate, Jain index, minimum served fraction, and the
5th-percentile served fraction in the dynamic per-epoch outputs.

`fairness_vs_periodic_gb` is allocation similarity, **not** exact max-min
fairness. Periodic GB has a score of 1.0 by definition. The separate Jain
and served-fraction metrics provide independent fairness measurements.

### Implemented instrumentation (allocation logic unchanged)

The following helper describes the implementation in
`extension\hybrid_hybrid_te.py`; it computes observed served/offered ratios and
does not alter allocations:

```python
    def _flow_metrics(self, demand, allocation):
        records = []
        fractions = []
        for src in range(demand.shape[0]):
            for dst in range(demand.shape[1]):
                if src == dst:
                    continue
                offered = float(demand[src, dst])
                served = float(np.sum(allocation.get((src, dst), ())))
                fraction = served / offered if offered > 1e-9 else None
                records.append({
                    "src": src,
                    "dst": dst,
                    "offered_demand": offered,
                    "served_rate": served,
                    "served_fraction": fraction,
                })
                if fraction is not None:
                    fractions.append(fraction)
        values = np.asarray(fractions, dtype=float)
        if values.size:
            squared_sum = float(np.sum(values * values))
            jain = (
                float(np.sum(values) ** 2 / (values.size * squared_sum))
                if squared_sum else float("nan")
            )
            minimum = float(np.min(values))
            p05 = float(np.quantile(values, 0.05))
        else:
            jain = minimum = p05 = float("nan")
        return {
            "per_flow_metrics_json": json.dumps(records, separators=(",", ":")),
            "jain_index": jain,
            "min_served_fraction": minimum,
            "p05_served_fraction": p05,
        }
```

The implementation adds these CSV fields: `per_flow_metrics_json`, `jain_index`,
`min_served_fraction`, and `p05_served_fraction`. The per-flow JSON includes
every off-diagonal OD pair; zero-offer pairs have a `null` fraction and are
excluded from Jain/minimum/5th-percentile calculations. Positive-offer flows
with zero service remain in the calculation with fraction zero. If all
positive-offer flows have zero service, Jain is undefined and is written as
NaN.

The fields and existing churn are propagated consistently:

1. `extension\compare_hybrid_baselines.py`
   - The output schema includes the new metrics and
     `allocation_changes`/`gb_reruns`.
   - `_baseline_metrics` merges in `experiment._flow_metrics(demand,
     allocation)` and sets `gb_reruns` to zero.
   - The controller rows preserve the fairness fields,
     `allocation_changes`, and `gb_reruns`.
   - Prior B4 and B4+ allocations are tracked by epoch to compute their
     allocation churn with the shared `_churn` helper.
   - The method summary includes the new scalar metrics. Per-flow JSON
     in raw epoch rows only.
2. `extension\run_uninett_sweep.py`
   - Its result schema comes from `comparison.RESULT_COLUMNS`; controller
     metric dictionaries preserve the new metrics and churn fields.
   - Per-case summaries include Jain, minimum fraction, p05 fraction, and
     allocation churn. Previous B4/B4+ allocations are checkpointed so resume
     does not reset their churn at an epoch boundary.
3. `extension\analyze_results.py`
   - `EPOCH_SUMMARY_METRICS` includes `jain_index`, `min_served_fraction`,
     `p05_served_fraction`, and `allocation_changes`, not the JSON column.
4. `extension\grid_search_elephant_mice.py`
   - Raw rows inherit the new fields from `Experiment.run_scenario`; its
     parameter summary includes the scalar fairness fields and churn. Its
     per-flow JSON remains in `per_epoch.csv`.
5. `hybrid_hybrid_te.py`'s legacy CSV writer schema includes the added fields;
   its non-sweep runner also reports scalar metrics in its aggregate output.

This patch only measures actual/offered ratios and existing allocation
changes; it does not alter any allocation, controller threshold, or
parameter. Do not interpret Jain index as a max-min optimality certificate.

### Smoke-test the implemented metrics

Use the Stage 0 normal-only command with a **new** output directory:

```powershell
$FairnessSmoke = "extension\uninett_runs\fairness_smoke_$Stamp"
& $Python extension\run_uninett_sweep.py --traffic-models uniform --scales 32 --scenarios normal --matrix-selection first --epochs 1 --num-paths 3 --path-processors 1 --controller-workers 1 --runtime-repeats 0 --max-cases 1 --output $FairnessSmoke
```

**Estimated time:** under 3 minutes with the cached k=3 paths.

**Expected output:** 9 per-epoch rows, including all nine methods; each row
has finite scalar fairness metrics (except the explicitly undefined all-zero
case) and parseable per-flow JSON. The summary and median summary have the
same 9 method rows. Capacity violations remain zero.

**Inspect before continuing:** validate one row per method, every JSON record
has `src`, `dst`, `offered_demand`, `served_rate`, and `served_fraction`,
and the computed ratios are in plausible bounds. Verify B4/B4+ churn columns
exist and the run config/checkpoint are created. Do not proceed to Stage 1 if
the schema differs or resume loses baseline state.

## Stage 1 — Matched Uninett hybrid/B4/B4+ screening

This is the repository's focused screening design: one sampled matrix for
each of four traffic models at scale 32, five workloads (normal plus all four
dynamic shifts), 15 epochs, 3 candidate paths, and 9 methods. The sample is
deterministic for seed 0. The independent fairness/churn patch from Stage 2
must be in place first.

**Working directory:** project directory.

```powershell
$Stage1 = 'extension\uninett_runs\uninett_stage1_seed0'
& $Python extension\run_uninett_sweep.py --topology Uninett2010.graphml --traffic-models uniform,bimodal,gravity,poisson-high-inter --scales 32 --scenarios normal,sudden_spike,hotspot_shift,dcn_incast,demand_drop --matrix-selection sample --matrices-per-stratum 1 --epochs 15 --seed 0 --num-paths 3 --path-processors 1 --controller-workers 1 --runtime-repeats 1 --output $Stage1
```

**Estimated time:** 2–4 hours. Existing 3-path timings imply roughly an hour
for one pass over 300 epoch groups; the extra serial runtime repeat is
approximately another pass. This estimate includes headroom for case and
solver variance, but is not a hard runtime limit. If this exceeds the
available session, interrupt at an epoch boundary and resume later.

**Expected files:** `per_epoch.csv`, `runtime_benchmarks.csv`, `summary.csv`,
`median_summary.csv`, `run_config.json`, `checkpoint.pkl`, and the six plots:
`unmet_demand_by_scenario.png`, `fairness_by_scenario.png`,
`runtime_by_topology.png`, `capacity_violations_by_scenario.png`,
`fairness_by_scale.png`, and `runtime_by_scale.png`.

Expected completed-run counts:

- 20 cases (4 traffic models × 1 sampled matrix × 5 scenarios).
- 300 distinct case-epochs (20 × 15).
- 2,700 per-epoch method rows (300 × 9 methods).
- 2,700 runtime benchmark rows with one separate timing repeat.
- 180 method/case rows in each of `summary.csv` and `median_summary.csv`.

Progress reports 300 quality epochs; separate timing progress is also shown.

### Exact resume command

Use the identical settings and output path; only append `--resume`:

```powershell
& $Python extension\run_uninett_sweep.py --topology Uninett2010.graphml --traffic-models uniform,bimodal,gravity,poisson-high-inter --scales 32 --scenarios normal,sudden_spike,hotspot_shift,dcn_incast,demand_drop --matrix-selection sample --matrices-per-stratum 1 --epochs 15 --seed 0 --num-paths 3 --path-processors 1 --controller-workers 1 --runtime-repeats 1 --output $Stage1 --resume
```

Do not change the seed, selected matrices, scenarios, epochs, path count,
runtime repeats, or output directory on resume.

### Rebuild the summary and graphs after completion

```powershell
& $Python extension\analyze_results.py --input "$Stage1\per_epoch.csv" --output-dir $Stage1
```

This regenerates `median_summary.csv` and the plots; it does not rerun
allocations.

**Estimated time:** under 2 minutes.

Summarize runtime median and p95 over the observed case-epoch timings:

```powershell
& $Python -c "import pandas as pd,sys; d=pd.read_csv(sys.argv[1]); print(d.groupby(['traffic_model','scenario','method'])['runtime_benchmark_s'].agg(median_s='median',p95_s=lambda x:x.quantile(0.95)).to_string())" "$Stage1\runtime_benchmarks.csv"
```

This p95 is across observed case-epoch timings, not across repeated timing
trials. The run has one separate timing repeat.

**Inspect before continuing:** ensure all row counts above are reached,
`run_config.json` records 20 cases and the intended method list, every
case-method has 15 epochs, all methods share the same matrix/scenario inputs,
and the capacity-violation count is zero or any nonzero value is investigated.
Compare unmet-demand share, independent fairness, per-flow tails, serial
runtime, churn, and capacity violations as separate columns. Runtime repeats
are one additional timing sample per case-epoch; do not present them as a
large repeat distribution.

**Decision gate:** select at most one hybrid for Stage 5 only if it improves
the predeclared quality metric without an unacceptable fairness, tail-service,
capacity, or runtime regression. If no hybrid improves the baseline, report
that finding and skip Stage 5 rather than searching until a favorable result
appears.

## Stage 3 — Profile hybrid runtime components

The existing CSV runtime is total controller allocation time. It does not
separately time forecast GB, AW correction, adaptive headroom, adaptive
escalation, or post-solver conversion/serialization. Do not infer a cause
from the total runtimes.

### First-pass profiler command

The existing comparator has no method-selection flag; it runs the controller
set together with B4/B4+. The command below uses one representative
Uninett/uniform/scale-32 sudden-spike case, 9 epochs, 3 paths, and serial
workers. `cProfile` adds overhead, so use its output only to locate hot
functions, not as the reported runtime benchmark.

```powershell
$ProfileOutput = 'extension\comparison_runs\stage3_profile_case'
& $Python -m cProfile -o 'extension\uninett_runs\hybrid_components.prof' extension\compare_hybrid_baselines.py --topology Uninett2010.graphml --traffic-model uniform --scale 32 --scenario sudden_spike --seed 0 --epochs 9 --num-paths 3 --path-processors 1 --controller-workers 1 --runtime-repeats 1 --output $ProfileOutput
& $Python -c "import pstats; pstats.Stats(r'extension\uninett_runs\hybrid_components.prof').strip_dirs().sort_stats('cumtime').print_stats('Allocator|Experiment|quantile|Problem|write')"
```

**Estimated time:** 5–20 minutes. **Expected outputs:** the comparator CSVs
and config in `$ProfileOutput`, plus `hybrid_components.prof`.

**Inspect before continuing:** examine calls and cumulative time for
`Allocator.gb`, `Allocator.aw`, `Experiment.allocate`, `_aw_delta`,
`demand_link_load`, `Problem`, quantile calculation, and CSV writes. This
first pass is a function profile, not yet a trustworthy decomposition of
forecast and escalation GB calls.

### Component measurements required

For exact attribution, add temporary timers (not allocation changes) around
these existing operations and record method, epoch, call count, and elapsed
seconds:

1. **Forecast GB solve:** the forecast `self.allocator.gb(...)` calls in
   `Experiment.allocate` for `hybrid_basic`, `hybrid_fixed`, and
   `hybrid_adaptive`.
2. **Reactive AW correction:** the AW call inside `_aw_delta`; separately
   record `_aw_delta` total time so its bookkeeping is visible.
3. **Adaptive headroom:** the error quantile/headroom update in
   `Experiment.run_scenario`, before controller timing starts.
4. **Escalation GB re-solve:** the `self.allocator.gb(actual, reserved)` call
   in the escalation branch of `hybrid_adaptive`, distinct from its forecast
   GB call.
5. **Conversion and serialization:** in `Allocator.gb`/`Allocator.aw`, time
   problem construction, allocator call, and allocation/path normalization
   as separate portions; separately time CSV append/report generation in
   `run_uninett_sweep.py`.

Run the same representative input once for `hybrid_basic`, `hybrid_fixed`,
and `hybrid_adaptive` (the existing comparator also runs other methods; do
not invent a method flag). Report measured timings and call counts, including
overhead outside controller timers. Do not claim a speed difference is
caused by GB, AW, headroom, escalation, or serialization until these timers
show it. Remove or disable temporary timing hooks before final quality runs,
or retain them consistently for every method and document their overhead.

**Decision gate:** if timings do not separate the requested phases or include
all three hybrids, improve the instrumentation and repeat this one case;
do not profile the full sweep.

## Stage 4 — Targeted elephant/mice parameter ablation

The grid search is serial internally and has no `--controller-workers` flag.
Use four configurations (2 × 2), 9 epochs, 3 paths, and one representative
training case. It compares periodic GB and Hybrid ML Elephant/Mice. This is
parameter selection, not training a policy.

### Select parameters on seed 0 / sudden spike

```powershell
$GridTrain = 'extension\grid_runs\stage4_train_seed0'
& $Python extension\grid_search_elephant_mice.py --topology Uninett2010.graphml --traffic-model uniform --scale 32 --scenario sudden_spike --seed 0 --epochs 9 --num-paths 3 --path-processors 1 --elephant-fractions 0.10,0.20 --capacity-fractions 0.70,0.90 --output $GridTrain
```

**Estimated time:** 10–30 minutes. **Expected files:** `per_epoch.csv` (72
rows: 4 settings × 2 methods × 9 epochs), `summary.csv` (8 rows), and
`grid_config.json`.

The grid-search script does not implement checkpoint/resume. If interrupted,
do not reuse a partial folder as complete; rerun with a fresh `--output`
folder and account for the added runtime against the 12-hour budget.

**Inspect before continuing:** compare unmet-demand share and the independent
fairness/tail metrics added in Stage 2, runtime, capacity violations, and
allocation churn. Compare every setting against periodic GB on the same
training case. Choose parameters using a metric and trade-off rule decided
before looking at the held-out result.

### Held-out check on seed 1 / hotspot shift

After inspecting `summary.csv`, set the two PowerShell variables to the
selected fractions (do not automatically choose the lowest training unmet
demand if it damages other metrics):

```powershell
$BestElephantFraction = '0.10'
$BestCapacityFraction = '0.70'
$GridHeldOut = 'extension\grid_runs\stage4_heldout_seed1'
& $Python extension\grid_search_elephant_mice.py --topology Uninett2010.graphml --traffic-model uniform --scale 32 --scenario hotspot_shift --seed 1 --epochs 9 --num-paths 3 --path-processors 1 --elephant-fractions $BestElephantFraction --capacity-fractions $BestCapacityFraction --output $GridHeldOut
```

Replace the example fractions with the training-selected values before
running. **Estimated time:** 3–10 minutes. Expected outputs: 18 per-epoch
rows (2 methods × 9 epochs), 2 summary rows, and config.

This held-out grid invocation also has no resume support; use a fresh output
folder if it must be repeated.

**Inspect before continuing:** compare the chosen setting against periodic GB
on this independent seed and different shift. Treat a loss on held-out data as
evidence against that choice; do not retune against the held-out result and
then call it validation. The result is a small ablation, not a learned or
generalizable policy.

**Decision gate:** only nominate this hybrid for Stage 5 if its held-out
quality/fairness trade-off supports it. Otherwise retain the unmodified
baseline configuration or skip elephant/mice confirmation.

## Stage 5 — Confirmation only if budget and Stage 1 support it

Prefer one independent Uninett sample per traffic model at scale 32 and seed
1, with the same five scenarios, 15 epochs, 3 paths, 9 methods, and one
separate serial timing repeat. This tests another sampled traffic matrix per
model and a different workload seed. The runner has no method-subset flag,
so it will also re-evaluate the other hybrids rather than only the selected
one; that is intentional for an internally matched comparison.

**Run only if** Stage 1 supports a candidate and the actual time remaining
under the 12-hour cap is at least five hours.

```powershell
$Stage5 = 'extension\uninett_runs\uninett_confirmation_seed1'
& $Python extension\run_uninett_sweep.py --topology Uninett2010.graphml --traffic-models uniform,bimodal,gravity,poisson-high-inter --scales 32 --scenarios normal,sudden_spike,hotspot_shift,dcn_incast,demand_drop --matrix-selection sample --matrices-per-stratum 1 --epochs 15 --seed 1 --num-paths 3 --path-processors 1 --controller-workers 1 --runtime-repeats 1 --output $Stage5
```

Exact resume command:

```powershell
& $Python extension\run_uninett_sweep.py --topology Uninett2010.graphml --traffic-models uniform,bimodal,gravity,poisson-high-inter --scales 32 --scenarios normal,sudden_spike,hotspot_shift,dcn_incast,demand_drop --matrix-selection sample --matrices-per-stratum 1 --epochs 15 --seed 1 --num-paths 3 --path-processors 1 --controller-workers 1 --runtime-repeats 1 --output $Stage5 --resume
```

Analyze after completion:

```powershell
& $Python extension\analyze_results.py --input "$Stage5\per_epoch.csv" --output-dir $Stage5
```

**Estimated time:** 2–4 hours. Expected row counts match Stage 1: 20 cases,
300 epochs, 2,700 per-epoch/runtime rows, and 180 method/case summary rows.

**Inspect before continuing:** confirm the selected matrix names in
`run_config.json` differ from the seed-0 sample where alternatives exist,
all matched methods share those exact inputs, and all epoch/method counts are
complete. Report seed-0 screening and seed-1 confirmation separately before
any combined summary; with only two sampled matrices per traffic model, use
the combined result as limited confirmation, not broad generalization.

**Final decision gate:** stop after Stage 5. Do not add Cogentco or GtsCe
within this 12-hour experiment budget. If the second Uninett sample changes
the result materially, report instability and plan a later multi-seed study
instead of claiming a winner.

## Metrics and reporting rules

Report the following independently, by scenario, traffic model, scale, and
controller; report the two seeds separately:

1. **Unmet-demand share:** total unmet demand divided by total offered demand.
2. **Independent flow fairness:** Jain index of per-OD served fractions on
   positive-offer flows, with the formula and denominator stated.
3. **Per-flow tails:** minimum and 5th-percentile served fraction, with
   zero-service positive-offer flows included.
4. **Runtime:** median and p95 of separately measured serial controller
   timings, with repeat count and whether the statistic is over epochs, cases,
   or repeated trials stated. One extra timing repeat is not enough to claim
   a stable run-to-run tail distribution.
5. **Allocation churn:** number of OD/path rate entries changed between
   consecutive epochs, separately for each controller.
6. **Capacity violations:** count of links/epochs exceeding the capacity
   tolerance; do not hide violations in an average.
7. **Reference similarity:** retain `fairness_vs_periodic_gb` as a separate
   allocation-similarity metric. Periodic GB's score of 1.0 is true by
   definition and is not an independent fairness result.

### Claims supported versus unsupported

- **Supported after the planned runs:** paired dynamic comparisons among the
  seven extension controllers and repository B4/B4+ on the exact same sampled
  Uninett workload, routes, capacities, and epochs; observed serial timing
  with stated repeat count; independent fairness and churn only after the
  Stage 2 patch and smoke pass.
- **Supported by existing PR2 data, separately:** static-matrix SWAN, GB,
  AW, and Equidepth Binner comparisons on the PR2 configuration and repeated
  inputs. Use only its Uninett rows when making Uninett-specific statements.
- **Unsupported:** a matched dynamic comparison of hybrids with SWAN or EB;
  a unified ranking that pools PR2 static and extension dynamic scores;
  a general winner claim from one seed/matrix; a claim that repository B4/B4+
  settings were learned or tuned; or a claim of state-of-the-art superiority
  without matched B4/B4+, compatible SWAN/EB comparisons, repeated timing,
  and independent fairness measures.
- Do not describe scale 64 as a monotonic continuation of scales 8/16/32:
  it is separately calibrated to near saturation in the existing workload
  design.
