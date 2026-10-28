# Progress Review II — Soroush versus SWAN

## Purpose of this document

This is a knowledge-transfer note for the project **Soroush versus SWAN for Max-Min Fair Traffic Engineering**. It explains the project as it exists today, in plain language: the problem being solved, the code and data flow, what was completed in Milestone 1, what has been implemented for Milestone 2, what the existing results mean, and what remains before the final report.

The current workspace contains measured results for both milestones. The newest Milestone 2 run is `progress_review_2/runs/20261010_185812/`: 48 matched topology/workload/scale scenarios, three repeats, and all nine configurations (SWAN, four GB alphas, AW, and three EB bin counts). Older timestamped folders are partial runs and must not be mixed into the full result.

## 1. Project in simple terms

Imagine a network as a road map. Each link has a fixed capacity, and thousands of source-destination pairs want to send traffic simultaneously. If every pair greedily sends as much as possible, a few large demands can consume a bottleneck while other demands receive almost nothing.

Traffic engineering decides which paths each demand should use and how much traffic should be placed on those paths. Our fairness target is **max-min fairness**:

1. Try to increase every demand together.
2. When a demand cannot increase because it reaches a bottleneck, protect it.
3. Continue increasing the remaining demands without reducing a protected lower-rate demand.

The project compares two approaches to this allocation problem:

- **SWAN** is the practical baseline. It repeats a multi-commodity-flow (MCF) optimization, gradually raises the allowed rate range, and freezes demands that cannot grow.
- **Soroush** is the faster framework from the NSDI 2024 paper. It reduces repeated optimization by using approximations and heuristics. In this project we evaluate its Geometric Binner, Adaptive Waterfiller, and Equidepth Binner allocators.

The central research question from the proposal is: **when networks and workloads become larger, does Soroush retain a meaningful runtime advantage over SWAN without losing too much allocation quality or fairness?**

## 2. Inputs, outputs, and the experiment pipeline

### Inputs

Each experiment is built from five matched inputs:

| Input | Meaning | Why it is kept identical within a comparison |
| --- | --- | --- |
| Topology | A GraphML network graph: nodes are routers/sites and edges are links. | A different graph changes bottlenecks and makes the comparison unfair. |
| Traffic matrix | A synthetic list of source-destination demands. | Both methods must try to serve exactly the same requests. |
| Traffic scale | Multiplies workload intensity; larger values create more contention. | It is the controlled way to test increasing load. |
| Candidate paths | The same `k` shortest paths offered to every source-destination pair. | One method must not get better routing options. |
| Link capacity | `1000.0` units for every link in the current experiments. | Capacity changes would alter every allocation result. |

The data is stored under `ncflow/topologies/topology-zoo/` and `ncflow/traffic-matrices/`. The Milestone 1 baseline used Uninett2010, uniform traffic, scales 8/16/32/64, and 16 candidate paths. The Milestone 2 plan adds Cogentco and GtsCe, plus bimodal, gravity, and poisson-high-inter traffic models.

### What happens in one paired run

1. The runner loads one topology and one traffic matrix.
2. Demands with zero traffic are removed and demand IDs are made consecutive. This prevents meaningless zero-demand entries from interfering with the allocation algorithms.
3. It computes the candidate shortest paths once for that topology/path-count combination and reuses them for every allocator.
4. It runs SWAN and saves its allocation, runtime, and timing breakdown.
5. It runs each Soroush allocator on the **same** problem and path set.
6. It calculates total flow, runtime, speedup, and fairness relative to the SWAN result.
7. For Milestone 2, the complete paired run is repeated three times. Raw runs are written first; medians and range are calculated only afterwards.
8. CSV tables and plots are written to a timestamped output folder.

### Outputs and how to explain them

| Output | Interpretation |
| --- | --- |
| Total allocated flow | Sum of all traffic allocated across all demands. Higher indicates more aggregate traffic carried, but it alone does not prove fairness. |
| Relative total flow | `method total flow / SWAN total flow`. `1.0` is identical to SWAN; above `1.0` is more aggregate traffic; below is less. |
| Runtime | End-to-end allocator time in seconds. It includes the work the allocator reports, not just a single solver call. Lower is better. |
| Speedup vs SWAN | `SWAN runtime / method runtime`. Greater than `1.0` means the method is faster than SWAN. |
| Fairness vs SWAN | A per-demand comparison of the method's allocation with SWAN's allocation. `1.0` means it matches SWAN exactly; lower values indicate a larger allocation difference. It is a comparison-to-baseline measure, not a proof of exact max-min fairness. |
| ECDF plot | Shows the fraction of scenarios at or below each metric value. It summarizes many matched scenarios without hiding poor cases. |
| Runtime-fairness Pareto plot | Shows the trade-off directly: left is faster and up is closer to SWAN's allocation. |

## 3. Algorithm knowledge transfer

### SWAN — the reference baseline

SWAN works in rounds. In each round it builds an MCF optimization model. Every demand receives a lower and upper allowed rate. The upper range grows geometrically using `alpha = 2`. If a demand cannot grow further because the relevant links are saturated, SWAN freezes that demand and continues with the remaining demands.

This repeated solve-and-freeze process is a good approximation to max-min fairness, but it becomes expensive with many demands and paths. It is used as the comparison reference in this project, not as a claim of an exact mathematical optimum at all scales.

### Soroush Geometric Binner (GB)

GB divides the possible rate assigned to each demand into geometric pieces, called bins. Lower-rate pieces receive more priority in one weighted MCF optimization. The intent is to preserve the “help the smallest flow first” behavior of max-min fairness without SWAN's repeated freeze loop.

The Milestone 2 parameter sweep varies GB's geometric multiplier (`alpha = 1.25, 1.5, 2, 4`). Smaller steps create finer bins and may better imitate fairness, while larger steps can reduce model complexity and runtime. This sweep measures that trade-off rather than assuming one setting is best.

### Soroush Adaptive Waterfiller (AW)

AW is the fast heuristic. It repeatedly distributes available link capacity across candidate paths, then changes path weights according to congestion. It does not need a full MCF solve in the same way as SWAN and GB, so it is expected to be much faster.

The expected trade-off is important: because AW is heuristic, it may move less traffic or differ more from SWAN's per-demand allocation at heavier load. That is a valid result if it is reported alongside its runtime advantage.

### Soroush Equidepth Binner (EB)

EB first uses waterfilling information to rank or group demands by their likely allocated rates. It then creates dynamic, roughly equal-depth groups and performs an MCF refinement. It is intended to sit between AW and GB: it uses heuristic guidance but still includes an optimization step to improve allocation quality.

The repository already contained EB code, but it could not run in the current Python stack because it imported `gurobi` instead of the installed `gurobipy` package and used NumPy's removed `np.int` alias. Those compatibility issues are fixed as part of Milestone 2.

## 4. Milestone 1 — validated matched baseline

### What was done

Milestone 1 converted the earlier SWAN-versus-SWAN smoke test into a real matched experiment. The implementation is in `progress_review_1/run_pr1_matched_baseline.py`, and the explanatory note is `PROGRESS_REVIEW_1.md`.

The benchmark ran:

- Topology: `Uninett2010.graphml`
- Traffic model: uniform
- Scales: 8, 16, 32, 64
- Candidate paths: 16 shortest paths per demand
- Link capacity: 1000.0
- Demands per matrix: 5,402
- Allocators: SWAN, Geometric Binner, and Adaptive Waterfiller

### Why this was done first

Before scaling across many topologies and traffic models, the team needed proof that the comparison itself was fair: the same matrix, paths, capacities, and scale must reach both SWAN and Soroush. Otherwise, a plot could look impressive while comparing different inputs.

The old smoke-test plots showed values of `1.0` because SWAN was compared with itself. That verified the plotting pipeline only. The Milestone 1 runner replaced this with real Soroush runs.

### Verified Milestone 1 result summary

| Metric across the four matched scenarios | Geometric Binner | Adaptive Waterfiller |
| --- | ---: | ---: |
| Median speedup versus SWAN | 2.68x | 9.71x |
| Median relative total flow | 1.002 | 0.903 |
| Median fairness relative to SWAN | 0.951 | 0.889 |

The simple explanation is:

- **GB** was roughly 2.4–2.8 times faster than SWAN while keeping total traffic nearly identical. Its allocation became somewhat less similar to SWAN as the load increased, but remained close overall in this small slice.
- **AW** was much faster, roughly 8–16 times faster, but its total flow and fairness comparison declined as load increased. This is the expected speed-versus-quality trade-off for a heuristic.

These are meaningful validation results, but they are **not** the final paper reproduction because they cover only one topology, one traffic model, one path count, four matrices, and one run each.

### Milestone 1 files created or changed

| File or folder | What changed and why |
| --- | --- |
| `PROGRESS_REVIEW_1.md` | Plain-language explanation of the verified Milestone 1 baseline and figures. |
| `progress_review_1/run_pr1_matched_baseline.py` | Runs SWAN, GB, and AW on identical inputs; writes the result table, JSON summary, timing logs, and plots. |
| `progress_review_1/pr1_matched_results.csv` | The four raw scenario summaries used for the Milestone 1 numbers. |
| `progress_review_1/pr1_matched_results.json` | Structured summary including timing breakdowns and median values. |
| `progress_review_1/figs/` | ECDF and scatter plots for flow, speedup, fairness, and runtime versus flow. |
| `traffic_engineering/alg/geometric_approx_binning.py` | Changed the Gurobi import to `gurobipy`, matching the installed solver package. |
| `traffic_engineering/utilities/utils.py` | Replaced fragile relative input paths with paths calculated from the repository location, so topology and matrix discovery works from the Windows workspace. |
| `traffic_engineering/benchmark/swan_micro_benchmark.py` | Adjusted local import/path setup so the SWAN micro-benchmark can locate project modules on the Windows machine. |

## 5. Milestone 2 — full evaluation implementation

### What was implemented

`progress_review_2/run_pr2_full_evaluation.py` is the reproducible Milestone 2 runner. Its default plan resolves 48 matched scenarios:

- 3 topologies: Uninett2010, Cogentco, GtsCe
- 4 traffic models: uniform, bimodal, gravity, poisson-high-inter
- 4 scales: 8, 16, 32, 64

For every scenario, it uses 16 paths, repeats measurements three times, runs SWAN plus four GB configurations and AW, and can additionally run EB with a three-value bin sweep. The default GB alpha sweep is 1.25, 1.5, 2, and 4. The optional EB bin sweep is 4, 8, and 16.

### Why the runner is designed this way

- **Repeated runs:** runtime naturally varies due to operating-system activity, caching, and solver behavior. Median, minimum, and maximum prevent one lucky measurement from becoming the conclusion.
- **Raw results before summaries:** `per_run_results.csv` preserves every measurement. The summary CSV can always be traced back to the underlying runs.
- **Deterministic matrix selection:** when several matrices exist for the same scale, the runner chooses the alphabetically first one and records its filename. This makes the selection repeatable.
- **Cached paths:** candidate paths are expensive to construct but do not need to be recomputed for every method on the same topology and path count. Reusing them also preserves fairness.
- **Config snapshot:** each result folder includes the exact command settings in `experiment_config.json`, allowing the run to be reproduced.

### Milestone 2 files changed

| File | Change | Reason |
| --- | --- | --- |
| `progress_review_2/run_pr2_full_evaluation.py` | New complete benchmark and reporting harness. | Implements the planned scaling study, GB sensitivity sweep, repeated measurements, and optional EB evaluation. |
| `progress_review_2/README.md` | New operational guide. | Explains preflight, smoke test, full runs, and every generated output. |
| `traffic_engineering/alg/approx_water_bet_plus_mcf.py` | Uses `gurobipy`; replaces `np.int` with built-in `int`. | Makes the EB wrapper compatible with the current Python/Gurobi/NumPy stack. |
| `traffic_engineering/alg/approx_water_plus_mcf.py` | Uses `gurobipy`; replaces `np.int` with built-in `int`. | Makes EB's MCF refinement compatible with the current stack. |

### What a successful Milestone 2 result folder contains

Each full run creates `progress_review_2/runs/<timestamp>/` containing:

| Output | Use in the report |
| --- | --- |
| `experiment_config.json` | Methods section: exact topology/model/scale/repeat/path/parameter choices. |
| `per_run_results.csv` | Appendix or reproducibility evidence: every paired repeat and timing breakdown. |
| `median_summary.csv` | Main quantitative tables and any aggregate analysis. |
| `speedup_ecdf.png` | Demonstrates how often each Soroush configuration is faster than SWAN. |
| `total_flow_ecdf.png` | Shows aggregate throughput relative to SWAN. |
| `fairness_ecdf.png` | Shows how closely each method tracks SWAN's allocations. |
| `runtime_fairness_pareto.png` | Main trade-off figure for the presentation and report. |

### Measured results and parameter choice

The newest complete run has 48 scenarios, 432 median-summary rows, and 1,296 raw runs. All 432 method/scenario groups have three repeats. The table below averages each scenario's repeat-median values equally across the 48 scenarios; speedup also includes the median across those scenarios. Fairness is the repository's geometric agreement score against SWAN, not an independent proof of max-min fairness.

| Configuration | Median speedup | Mean speedup | Relative total flow | Mean fairness | Worst scenario fairness | Mean runtime (s) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| GB, alpha=1.25 | 2.29x | 2.35x | 0.9978 | 0.9288 | 0.8026 | 23.18 |
| GB, alpha=1.5 | 2.52x | 2.45x | 1.0006 | 0.9252 | 0.7931 | 25.83 |
| **GB, alpha=2** | **2.89x** | **2.76x** | **1.0083** | **0.9439** | **0.8383** | **22.32** |
| GB, alpha=4 | 2.57x | 2.67x | 1.0193 | 0.8953 | 0.6640 | 28.65 |
| AW, water=1; bet=10 | 13.36x | 14.79x | 0.9302 | 0.9041 | 0.7480 | 3.57 |

**Recommendation: use GB alpha=2 as the default.** It has the best median and mean speedup among the GB settings, highest mean fairness, and lowest mean runtime in this sweep. Alpha=4 carries more total flow, but is less faithful to SWAN and has a worst-scenario fairness of 0.664; it is not the balanced default. AW is the speed-oriented option: about 14.8x mean speedup, with about 7% lower mean total flow and lower fairness agreement than GB alpha=2.

GB alpha=2 also had the best mean fairness among the four GB settings in each of the four traffic models. In Poisson high-inter traffic, its mean fairness was 0.929, versus 0.858 for alpha=4. This supports alpha=2 beyond the overall average. The AW sweep, however, contains only one setting (`water=1; bet=10`); there is no evidence here to claim it is an optimal AW parameter choice.

Read the generated figures in the same folder: `speedup_ecdf.png` shows AW's clear speed advantage; `total_flow_ecdf.png` shows alpha=4 shifted toward more total flow and AW below SWAN; `fairness_ecdf.png` puts alpha=2 furthest toward SWAN agreement among GB settings; and `runtime_fairness_pareto.png` shows the trade-off, though its 48 points per configuration make it dense.

Two raw runtime observations exceed 500 seconds: SWAN on Cogentco/uniform/scale 64 (1,310.7 s) and GB alpha=4 on Cogentco/bimodal/scale 16 (2,581.9 s). The summary uses the median of three repeats, limiting the effect of these outliers, but the raw range should be retained and the solver/runtime anomalies investigated before making tail-latency claims. Repeats reuse the same traffic matrix and are runtime repetitions, not independent workload samples.

### Extension results are a separate smoke test

`extension/results.csv` contains 72 rows: six controllers over four synthetic scenarios and three epochs each. It uses a five-node toy graph, one seed per scenario, and only three epochs, so it does not extend PR2 scale or establish performance on the benchmark topologies. In this sample, actual-demand AW, periodic GB, and all hybrids have identical served demand and fairness; their average unmet demand is zero. Forecast-based GB has total unmet demand of about 266 units across the 12 scenario-epochs, concentrated in the sudden-spike, hotspot-shift, and near-saturation scenarios because its moving-average forecast lags those changes. It no longer overserves after the demand-drop scenario: forecast GB is capped at each commodity's actual demand. The adaptive controller reports zero GB reruns in every epoch, so its escalation branch has not been exercised.

The extension's fairness value is agreement with periodic GB on actual demand. Its capacity-violation count is guarded by an assertion that stops the run if a violation occurs, so zero means the guard passed, not a separately estimated violation rate. The first epoch's allocation changes also count initial allocation from an empty state. The first GB invocation includes about 2.75 seconds of Gurobi startup; later epochs run in milliseconds, so the runtime chart uses per-method medians and the CSV should be used when studying cold-start latency. Treat these charts as diagnostics for controller wiring, not final comparative evidence. See `extension/README.md` for the detailed limitations.

## 6. Current status and remaining work

### Verified state

1. Milestone 1 has four matched Uninett2010/uniform cases.
2. The `20261010_185812` Milestone 2 folder contains the complete 48-scenario, three-repeat suite, including EB. Its configuration and raw/summary CSVs agree on coverage.
3. The measured sweep supports GB alpha=2 as the balanced default; AW is appropriate when speed is prioritized over total flow and fairness agreement.
4. The extension charts come from its small synthetic smoke run, not the PR2 evaluation.

Remaining work: investigate the two extreme runtime observations, sweep AW's bet-iteration setting (the main PR2 data contains only `water=1; bet=10`), run a longer/more severe extension workload that exercises adaptive reruns, and carry the stated limitations into the final report. The extension plotter now uses Matplotlib directly because the installed Seaborn/Matplotlib combination failed during import.

## 7. Remaining work against the proposal

| Proposal commitment | Current state | Remaining work |
| --- | --- | --- |
| Matched Soroush-versus-SWAN baseline | Complete and verified for four Uninett2010/uniform cases. | Keep it as a regression/smoke test. |
| Topology and workload scaling | Measured across 48 combinations. | Investigate runtime outliers and preserve the run artifacts. |
| Uniform, bimodal, gravity, and Poisson traffic | Measured in the newest run. | Keep workload-specific results and caveats in the final report. |
| At least three repeats and median reporting | Three repeats present for every result group. | Treat repeats as runtime repeats on the same matrix, not independent samples. |
| GB parameter sensitivity | Alpha sweep measured. | Use alpha=2 as the balanced default; document alpha=4's throughput/fairness trade-off. |
| Adaptive Waterfiller comparison | Measured across 48 scenarios at `water=1; bet=10`. | Present its speed advantage alongside lower total flow/fairness agreement; sweep AW settings before claiming an optimal value. |
| Equidepth Binner | Three EB bin counts measured. | Include its runtime/quality trade-off if EB is part of the final story. |
| Combined Pareto analysis | Figures generated from the full run. | Use the plot with the quantitative table; the 48-point scatter is dense. |
| Final report and reproducibility guide | Initial guide and measured PR2 analysis exist. | Incorporate findings, outliers, and limitations into the final submission. |

## 8. Suggested viva / presentation explanation

Use this sequence when explaining the work:

1. “We study fair sharing of limited network capacity across competing traffic demands.”
2. “SWAN repeatedly solves an optimization problem to approximate max-min fairness. Soroush tries to achieve similar allocations more quickly.”
3. “We made comparisons fair by holding topology, traffic matrix, candidate paths, and capacities constant for every SWAN-Soroush pair.”
4. “Milestone 1 verified the pipeline with real matched runs. GB retained near-SWAN throughput at about 2.7x median speedup; AW was about 9.7x faster but lost more quality under load.”
5. “Milestone 2 expands this to different topologies, traffic types, load scales, repeated measurements, and parameter sensitivity. We added the missing EB path and make every experiment reproducible through raw CSVs and configuration snapshots.”
6. “The full planned suite has been measured. GB alpha=2 is the balanced default in this sweep; AW is the faster but lower-throughput choice. We will retain runtime outliers and clarify that the synthetic extension is still a smoke test.”

## 9. Commands to continue

From the repository root, after restoring the Python/Gurobi environment:

```powershell
# See exactly what will run; no solver execution.
python progress_review_2\run_pr2_full_evaluation.py --plan

# Confirm the environment on one small matched case.
python progress_review_2\run_pr2_full_evaluation.py --topologies Uninett2010.graphml --traffic-models uniform --scales 8 --repeats 1 --max-scenarios 1

# Run GB and AW over the default Milestone 2 plan.
python progress_review_2\run_pr2_full_evaluation.py

# Add Equidepth Binner and its bin sensitivity sweep.
python progress_review_2\run_pr2_full_evaluation.py --include-eb
```

After a full run, inspect `median_summary.csv` first, then check the ECDF and Pareto plots. If a configuration fails, preserve its terminal error and output folder; do not silently omit it from the final analysis.
