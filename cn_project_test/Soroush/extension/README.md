# Predictive hybrid traffic engineering

`hybrid_hybrid_te.py` evaluates seven controllers over the Milestone 2
topology, traffic-model, scale, and dynamic-shift grid:

- Topologies: `Uninett2010.graphml`, `GtsCe.graphml`, and `Cogentco.graphml`.
- Traffic models: `uniform`, `bimodal`, `gravity`, and `poisson-high-inter`.
- Load scales: 8, 16, 32, and 64. At scale 64, each baseline is calibrated
  to 97% of the maximum equal-split candidate-path link load.
- Dynamic shifts: `sudden_spike`, `hotspot_shift`, `dcn_incast`, and
  `demand_drop` (65% demand reduction).

The default run generates 15 continuous epochs per case and uses `k=16`
candidate paths. Path outputs are cached by topology and path count in
`.path-cache/`, so each topology's paths are computed once and reused across
traffic matrices. Increase the experiment length to 20 epochs with
`--epochs 20`. This complete grid is compute-intensive: the full-matrix GB
controllers still solve every epoch, particularly on the large topologies.

Run from this directory:

```powershell
python hybrid_hybrid_te.py
```

By default, all three topologies run across 4 traffic models × 4 scales × 4
shifts = 192 scenarios, with 15 epochs each. For a Uninett-only run, specify
`--topologies Uninett2010.graphml`; this selects 64 scenarios (960 epochs).
Use `--traffic-models`, `--scales`, and `--scenario` to narrow the workload.
`--max-scenarios N` limits the ordered topology/model/scale matrix cases (each
selected case still runs every selected shift). `--num-paths`,
`--path-processors`, `--controller-workers`, `--cache-dir`, `--seed`, and
`--epochs` control path generation, controller concurrency, repeatability, and
duration.

The runner shows a progress bar with completed/total epochs, completed
scenarios, elapsed time, and an estimated time remaining. Controller runs
within each epoch use a thread pool (default 2 workers; change with
`--controller-workers`). Only the main thread writes results, after the full
epoch is complete, and flushes the CSV at that boundary. Thus `results.csv`
contains recoverable completed epochs if the run is interrupted. An atomic
checkpoint beside the CSV stores the controller and classifier state required
to continue in the middle of a scenario. Resume with the same run settings and
output path:

```powershell
python hybrid_hybrid_te.py --topologies Uninett2010.graphml --output results_uninett.csv --resume
```

The output is overwritten when a new run starts without `--resume`; pass
`--output another-file.csv` to preserve a previous run. Resume requires both
the CSV and its matching `.checkpoint.pkl` file. A CSV created before
checkpoint support was added cannot be resumed exactly on its own.

Each CSV row contains per-epoch served/unmet demand, fairness against periodic
GB, capacity violations, runtime, allocation churn, topology, traffic model,
scale, and shift. Per-OD offered demand, served rate, and served fraction are
stored in `per_flow_metrics_json`; Jain index, minimum served fraction, and
the 5th-percentile served fraction are separate scalar columns. The flow
fairness metrics use positive-offer OD pairs, include zero-served flows, and
are independent of the allocation-similarity score against periodic GB.
`fairness_vs_periodic_gb` is not exact max-min fairness, and periodic GB's
score of 1.0 is true by definition. B4/B4+ churn is measured against the
previous epoch and preserved across sweep checkpoints.

Run `analyze_results.py` after the experiment to generate plots and a separate
`median_summary.csv`, with median epoch metrics for each scenario/controller,
similar to the PR2 output structure. The raw `results.csv` remains per-epoch
data and is not mixed with summary metrics. Scenario plots aggregate the epoch
measurements by mean; topology runtime comparisons use median runtime. Runs
created with the earlier CSV schema cannot be resumed with the new schema;
use a new output file/directory for a fresh run.
Scenario plots aggregate the epoch measurements by mean; topology runtime
comparisons use median runtime.

The controllers are:

1. `gb_only`: GB using the moving-average forecast.
2. `aw_only`: AW using actual demand.
3. `periodic_gb`: GB using actual demand each epoch.
4. `hybrid_basic`: full-capacity forecast GB plus reactive AW.
5. `hybrid_fixed`: forecast GB with 10% fixed headroom plus reactive AW.
6. `hybrid_adaptive`: forecast GB with an 8-epoch, 90th-percentile
   headroom estimate and escalation re-solve.
7. `hybrid_ml_elephant_mice`: GB on the predicted top 15% of active flows,
   initially using 85% of link capacity; AW serves predicted mice from residual
   capacity, then reacts to positive actual-versus-predicted demand. The flow
   fraction and elephant capacity fraction are separate parameters.

`classifier.py` implements an online quantile classifier using EWMA demand
(`beta=0.7`), first-order momentum, rolling variance (`W=5`), and endpoint
degree centrality. Its prediction is updated only after an epoch is observed.
The experiment's moving-average forecast and adaptive headroom window are
both eight epochs.

Run a small serial comparison with the split configurable:

```powershell
python hybrid_hybrid_te.py --topologies Uninett2010.graphml --traffic-models uniform --scales 8 --scenario sudden_spike --controller-workers 1 --output baseline_small.csv
```

Sweep the two split parameters against periodic GB on the same case:

```powershell
python grid_search_elephant_mice.py --elephant-fractions 0.10,0.15,0.20 --capacity-fractions 0.70,0.85,0.95
```

The grid search writes raw epoch metrics, median summaries, and a config file
under `grid_runs/`. It is a parameter search, not a trained policy; use held-out
seeds and scenarios before claiming the chosen split generalizes.

Compare all seven extension controllers with the repository's B4/B4+
implementations on one shared workload, path set, and capacity:

```powershell
python compare_hybrid_baselines.py --topology Uninett2010.graphml --traffic-model uniform --scale 8 --scenario sudden_spike --controller-workers 1 --runtime-repeats 3
```

The comparison writes per-epoch and median CSVs plus a config under
`comparison_runs/`. Runtime is measured in separate serial repeats; B4/B4+ are
the repository implementations of the WAN baseline, not learned or tuned
settings. The results describe only the selected case and do not establish
generalization.

For a practical first research screening run, the default suite samples one
traffic matrix for each of the four traffic models at scale 32, then tests
`normal` plus all four dynamic scenarios. It compares all seven extension
controllers plus repository B4 and B4+ (20 matched cases, 15 epochs each).
The defaults use three candidate paths. All methods still use the same paths
and inputs within each case. Each controller's runtime is measured separately
during the serial quality pass, avoiding a second allocation run. For more
stable timing measurements at roughly twice the allocation cost, add
`--runtime-repeats 1`. `--controller-workers 1` is required:

```powershell
python run_uninett_sweep.py --output uninett_screening
```

The runner writes `per_epoch.csv` after every completed epoch and an atomic
`checkpoint.pkl`, so a failure can be resumed with exactly the same arguments:
separate terminal progress bars show quality-epoch and repeated-runtime
progress, with an ETA for the remaining work.

```powershell
python run_uninett_sweep.py --output uninett_screening --resume
```

Do not change the workload, epoch, path, matrix-selection, sample count, seed,
or repeat settings when resuming. Each epoch uses the same demands, paths, and
capacities for all methods; fairness is measured against periodic GB on actual
demand. The `normal` case uses the ordinary noisy workload without a controlled
shift. Sampling is deterministic for the supplied seed. This is a screening
study, not a final statistical claim: use more matrices and more paths for
follow-up confirmation.

To increase load coverage to 16, 32, and 64 (60 cases with one sampled matrix
per model/scale), use:

```powershell
python run_uninett_sweep.py --scales 16,32,64 --output uninett_load_screening
```

For the full available Uninett matrix suite at all four standard scales (400
cases), use `--scales 8,16,32,64 --matrix-selection all`. This larger sweep is
intended for extended compute time.

On completion, the runner creates `summary.csv`, `median_summary.csv`, and
`runtime_benchmarks.csv`, plus graphs in the output directory: unmet demand,
fairness, runtime by topology, capacity violations, fairness by scale, and
runtime by scale. `per_epoch.csv` records the per-controller serial runtime. The benchmark CSV
contains runtime medians from the separate serial repeats when enabled, or
copies the quality-pass timing when `--runtime-repeats 0`; report graphs use it
when available. The plots
summarize the CSVs; they do not imply a target was achieved. To regenerate the
reports after inspecting or changing the CSVs:

```powershell
python analyze_results.py --input uninett_full_run\per_epoch.csv --output-dir uninett_full_run
```

For a quick pipeline/resume validation, select one normal-workload case:

```powershell
python run_uninett_sweep.py --traffic-models uniform --scales 32 --scenarios normal --matrix-selection first --epochs 1 --num-paths 3 --path-processors 1 --controller-workers 1 --runtime-repeats 1 --output uninett_smoke
```

B4/B4+ refer to the repository implementations of the established WAN
baseline; their settings are not described as learned. Results from one
topology or run should not be generalized beyond the measured cases.

The existing TE GB/AW adapters accept a scalar link capacity. The runner
therefore uses uniform 1000 Mbps links, matching the Topology Zoo loader.
Adaptive GB reserves the most conservative per-link headroom (the maximum
estimated link error) because the GB interface does not accept per-link
capacity limits.
