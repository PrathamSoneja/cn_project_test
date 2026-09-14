# Progress Review II evaluation

`run_pr2_full_evaluation.py` is the Milestone 2 experiment harness. It extends the matched Progress Review I baseline from one topology and four uniform matrices to controlled, repeated comparisons across topologies, traffic models, load scales, Geometric Binner parameters, Adaptive Waterfiller, and (when enabled) Equidepth Binner.

Every row is paired with a SWAN run on the same traffic-matrix file, topology, link capacity, candidate-path set, and repeat. The runner saves raw runs before calculating median, minimum, and maximum values; do not report a result from a partial folder as the full evaluation.

## Preflight

The checked-in local `.venv` currently points to a removed Python 3.11 installation, so recreate it before the first solver run. Use a Python version supported by your Gurobi installation, install the project dependencies and `gurobipy`, then activate the Gurobi academic/WLS license. This environment repair is intentionally separate from the benchmark runner.

Print the matrices that will be included without running Gurobi:

```powershell
.\.venv\Scripts\python.exe progress_review_2\run_pr2_full_evaluation.py --plan
```

The default suite requests Uninett2010, Cogentco, and GtsCe; uniform, bimodal, gravity, and poisson-high-inter traffic; scales 8, 16, 32, and 64; three repeats; 16 paths; and four GB alpha values. Missing matrix combinations are skipped and visible in the plan.

## Run a smoke slice

```powershell
.\.venv\Scripts\python.exe progress_review_2\run_pr2_full_evaluation.py --topologies Uninett2010.graphml --traffic-models uniform --scales 8 --repeats 1 --max-scenarios 1
```

## Run the complete Milestone 2 suite

Start with GB and AW:

```powershell
.\.venv\Scripts\python.exe progress_review_2\run_pr2_full_evaluation.py
```

Include the paper's Equidepth Binner sensitivity sweep as well:

```powershell
.\.venv\Scripts\python.exe progress_review_2\run_pr2_full_evaluation.py --include-eb
```

Each invocation creates a timestamped folder in `progress_review_2/runs/` containing:

- `experiment_config.json` - exact resolved command configuration.
- `per_run_results.csv` - raw paired repetitions and timing breakdowns.
- `median_summary.csv` - median, minimum, and maximum across repeats.
- ECDF plots for speedup, relative total flow, and fairness plus a runtime-fairness Pareto scatter plot.

The Equidepth path previously used an obsolete `gurobi` module name and NumPy's removed `np.int` alias. Both were corrected in the allocator modules so the paper's third allocator is compatible with the current dependency versions once the environment is restored.
