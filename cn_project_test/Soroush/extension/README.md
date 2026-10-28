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
scale, and shift. Run `analyze_results.py` after the experiment to generate
plots and a separate `median_summary.csv`, with median epoch metrics for each
scenario/controller, similar to the PR2 output structure. The raw
`results.csv` remains per-epoch data and is not mixed with summary metrics.
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
7. `hybrid_ml_elephant_mice`: GB on the predicted top 15% of flows using
   85% reserved capacity, AW for predicted mice over remaining capacity, and
   reactive AW for positive actual-versus-predicted demand.

`classifier.py` implements an online quantile classifier using EWMA demand
(`beta=0.7`), first-order momentum, rolling variance (`W=5`), and endpoint
degree centrality. Its prediction is updated only after an epoch is observed.
The experiment's moving-average forecast and adaptive headroom window are
both eight epochs.

Generate the three requested figures and median summary from the per-epoch CSV:

```powershell
python analyze_results.py
```

This writes `unmet_demand_by_scenario.png`, `fairness_by_scenario.png`, and
`runtime_by_topology.png`. The unmet-demand plot uses scale-64 records when
available; the fairness and runtime plots aggregate by shift and topology.
Figures report measured results and do not imply a fairness or runtime target
was achieved unless the resulting CSV supports that conclusion.

The existing TE GB/AW adapters accept a scalar link capacity. The runner
therefore uses uniform 1000 Mbps links, matching the Topology Zoo loader.
Adaptive GB reserves the most conservative per-link headroom (the maximum
estimated link error) because the GB interface does not accept per-link
capacity limits.
