# Progress Review II — Soroush versus SWAN

## Purpose of this document

This is a knowledge-transfer note for the project **Soroush versus SWAN for Max-Min Fair Traffic Engineering**. It explains the project as it exists today, in plain language: the problem being solved, the code and data flow, what was completed in Milestone 1, what has been implemented for Milestone 2, what the existing results mean, and what remains before the final report.

The important distinction is this:

- **Milestone 1 has verified experimental results.** They compare SWAN, Geometric Binner, and Adaptive Waterfiller on four matched Uninett2010/uniform-traffic scenarios.
- **Milestone 2 has a complete reproducible experiment framework, including Equidepth Binner, but its full 48-scenario solver run has not yet been executed on the current machine.** The local Python virtual environment must first be rebuilt because it refers to a removed Python installation.

This document must not be read as claiming that the full paper-scale Milestone 2 results have already been measured.

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

## 6. Current status and immediate blocker

### Where the project is now

The project is between implementation completion and full data collection:

1. The Windows-compatible input discovery and SWAN/GB/AW matched baseline are working and produced verified Milestone 1 data.
2. The Milestone 2 runner, expanded scenarios, repetitions, parameter sweeps, plots, and EB compatibility fixes are implemented.
3. Scenario discovery has been checked: the default command resolves 48 usable topology-model-scale combinations.
4. Python syntax validation and a no-solver scenario-selection test passed.
5. The full solver suite has not been run since the current `.venv` references a Python 3.11 executable that is no longer installed at its recorded location.

### What must happen before claiming Milestone 2 results

Rebuild the local virtual environment, install the requirements and `gurobipy`, activate a valid Gurobi license, then run a one-scenario smoke test. Only after the smoke test completes should the full suite be launched. The exact commands are documented in `progress_review_2/README.md`.

Do not copy Milestone 1 numbers into a Milestone 2 table. They answer a smaller question and only support the claim that the comparison pipeline is valid.

## 7. Remaining work against the proposal

| Proposal commitment | Current state | Remaining work |
| --- | --- | --- |
| Matched Soroush-versus-SWAN baseline | Complete and verified for four Uninett2010/uniform cases. | Keep it as a regression/smoke test. |
| Topology and workload scaling | Framework complete. | Run the 48 scenarios and inspect failures/timeouts. |
| Uniform, bimodal, gravity, and Poisson traffic | Framework complete. | Execute all planned traffic-model combinations. |
| At least three repeats and median reporting | Framework complete. | Collect the three actual measurements per configuration. |
| GB parameter sensitivity | Framework complete: alpha sweep. | Analyze whether finer bins improve fairness enough to justify runtime. |
| Adaptive Waterfiller comparison | Present in Milestone 1 and framework. | Confirm its trade-off across large scenarios. |
| Equidepth Binner | Compatibility work and framework support complete. | Run `--include-eb`; diagnose any solver/model failures on large cases. |
| Combined Pareto analysis | Plot generation complete. | Interpret the full measured figures and identify non-dominated settings. |
| Final report and reproducibility guide | Initial operational guide exists. | Write final findings only after the full outputs are available; include limitations and exact commands. |

## 8. Suggested viva / presentation explanation

Use this sequence when explaining the work:

1. “We study fair sharing of limited network capacity across competing traffic demands.”
2. “SWAN repeatedly solves an optimization problem to approximate max-min fairness. Soroush tries to achieve similar allocations more quickly.”
3. “We made comparisons fair by holding topology, traffic matrix, candidate paths, and capacities constant for every SWAN-Soroush pair.”
4. “Milestone 1 verified the pipeline with real matched runs. GB retained near-SWAN throughput at about 2.7x median speedup; AW was about 9.7x faster but lost more quality under load.”
5. “Milestone 2 expands this to different topologies, traffic types, load scales, repeated measurements, and parameter sensitivity. We added the missing EB path and make every experiment reproducible through raw CSVs and configuration snapshots.”
6. “The remaining task is data collection and analysis, not design: rebuild the local environment, execute the planned suite, then report only measured results.”

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
