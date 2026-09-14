# Progress Review I — What we did, in simple language

This note is the Progress Review I write-up for **CS G525: Soroush vs SWAN for max-min fair traffic engineering**. It explains the problem, the code we ran, and how to read the first matched overlay plots.

**Review target (from the proposal):** a validated baseline and the first matched Soroush-versus-SWAN overlay plot.

---

## The idea in one paragraph

A network has limited link capacity. Many source–destination pairs all want as much bandwidth as they can get. **Traffic engineering** decides how to share that capacity. **Max-min fairness** is a sharing rule that protects small flows: you should not give extra bandwidth to a large demand if that extra would take bandwidth away from a demand that already has less (or the same).

**SWAN** (SIGCOMM 2013) approximates that rule by solving several optimization problems in a loop, raising a rate ceiling in geometric steps and freezing flows that cannot grow further.

**Soroush** (NSDI 2024) tries to get a similar allocation **much faster**. Instead of many repeated optimizations, it uses:

- **Geometric Binner (GB):** one optimization whose variables are split into geometrically spaced rate bins.
- **Adaptive Waterfiller (AW):** a fast heuristic that repeatedly “pours” bandwidth onto paths, adjusting path weights as it goes.

This review compares those methods on **the same inputs**, so a speed or quality difference is due to the algorithm, not a different topology or traffic file.

---

## What “matched” means

Every SWAN–Soroush pair used:

| Knob | Value used for Review I |
| --- | --- |
| Topology | `Uninett2010.graphml` (Internet Topology Zoo) |
| Traffic pattern | uniform synthetic traffic matrices |
| Scale factors | 8, 16, 32, 64 (one matrix each) |
| Candidate paths | the **same** 16 shortest paths per pair |
| Link capacity | 1000.0 units on every edge |
| Demands per matrix | 5402 source–destination pairs |
| Machine | local Windows 11 workstation, Gurobi academic license |

That is the Progress Review I baseline: not the full paper-scale sweep yet, but a real head-to-head on identical instances.

---

## What is happening in the code

The runner is:

`progress_review_1/run_pr1_matched_baseline.py`

In order, it:

1. **Finds four traffic matrices** under `ncflow/traffic-matrices/uniform/` whose names start with `Uninett2010` and whose scale is 8, 16, 32, or 64.
2. **Computes 16 shortest paths** once on the Uninett2010 graph and reuses that path set for every algorithm and every matrix.
3. **Runs SWAN.** SWAN builds a multi-commodity flow (MCF) model and solves it several times. In iteration `k` every unfrozen demand must send at least a lower bound and at most an upper bound. Those bounds grow as \(U, \alpha U, \alpha^2 U, \ldots\) with \(\alpha = 2\). When a demand cannot grow anymore, it is frozen at its current rate.
4. **Runs Geometric Binner (Soroush).** It puts each demand’s rate into geometric bins and solves **one** weighted MCF. Smaller rate pieces are weighted more heavily, which encourages max-min-like sharing without a long freeze loop.
5. **Runs Adaptive Waterfiller (Soroush).** This does **not** call Gurobi. It waterfills on the path graph for a few iterations (`num_iter_approx=1`, `num_iter_bet=10`). It is the speed extreme of Soroush.
6. **Records** total allocated traffic, wall-clock time, and a fairness number against SWAN.
7. **Draws overlay plots** in `progress_review_1/figs/`.

A small code fix was required for Geometric Binner: `traffic_engineering/alg/geometric_approx_binning.py` imported `gurobi`, which is not the installed package. It now imports `gurobipy`, matching the working SWAN solver.

---

## How to read the plots

All four figures overlay **the same four matched scenarios**.

### 1. Total flow relative to SWAN

File: `progress_review_1/figs/pr1_total_flow_relative_to_swan.png`

The x-axis is \(\frac{\text{total traffic allocated by the method}}{\text{total traffic allocated by SWAN}}\).

- **1.0** means the method moved the same total volume as SWAN.
- **Above 1.0** means more total throughput than SWAN.
- **Below 1.0** means less total throughput than SWAN.

The y-axis is the fraction of the four scenarios at or below that ratio (an ECDF).

**What we saw:** Geometric Binner stays at or slightly above 1.0 (about 1.00–1.02). Adaptive Waterfiller is close at light load and drops toward about 0.85 at the heaviest scale.

### 2. Speedup relative to SWAN

File: `progress_review_1/figs/pr1_speedup_relative_to_swan.png`

The x-axis is \(\frac{\text{SWAN runtime}}{\text{method runtime}}\) on a log scale.

- **Greater than 1** means faster than SWAN.

**What we saw:** Geometric Binner is about **2.4×–2.8×** faster. Adaptive Waterfiller is about **9×–16×** faster.

### 3. Runtime vs total flow (the overlay scatter)

File: `progress_review_1/figs/pr1_runtime_vs_total_flow.png`

Each marker is one matched scenario. Left is faster; up is more total traffic. This is the first real Soroush-versus-SWAN overlay, not a SWAN-vs-itself smoke test.

**What we saw:** SWAN sits on the right (slower). Geometric Binner sits left of SWAN at almost the same height (similar volume, less time). Adaptive Waterfiller sits far left and a bit lower (fastest, less volume as load grows).

### 4. Fairness relative to SWAN

File: `progress_review_1/figs/pr1_fairness_relative_to_swan.png`

For each demand we compare the two allocated rates (with a small floor so zeros do not explode). The number is 1.0 when every demand gets the same rate as SWAN, and it falls as the two allocations disagree.

**What we saw:** Geometric Binner stays high (about 0.92–1.00). Adaptive Waterfiller is almost identical at scale 8 and falls to about 0.83 at scale 64.

---

## Numbers from the matched run

| Scale | SWAN flow | SWAN time (s) | GB flow | GB time (s) | GB speedup | AW flow | AW time (s) | AW speedup |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 8 | 9930 | 8.66 | 9930 | 3.36 | 2.58× | 9900 | 0.96 | 8.99× |
| 16 | 18847 | 8.20 | 18865 | 2.95 | 2.78× | 17142 | 1.01 | 8.08× |
| 32 | 27319 | 10.73 | 27416 | 4.42 | 2.43× | 24468 | 1.03 | 10.44× |
| 64 | 34166 | 11.92 | 35008 | 4.28 | 2.79× | 28984 | 0.75 | 15.90× |

Medians across the four scenarios:

- Geometric Binner speedup ≈ **2.68×**, relative total flow ≈ **1.002**, fairness vs SWAN ≈ **0.95**
- Adaptive Waterfiller speedup ≈ **9.71×**, relative total flow ≈ **0.90**, fairness vs SWAN ≈ **0.89**

The raw table is `progress_review_1/pr1_matched_results.csv`. Per-path rates and solver timings are pickled under `progress_review_1/logs/`.

---

## What this does *not* claim yet

These four uniform Uninett2010 matrices are a **validated matched baseline**, not the final paper reproduction.

Still open for later reviews (as planned in the proposal):

- Other traffic models (bimodal, gravity, Poisson)
- Larger topologies (Cogentco, GtsCe, and so on)
- Three repeats per configuration and median reporting
- Geometric Binner bin-parameter sweep
- Full Equidepth Binner / APPROX+MCF Soroush variant
- Combined Pareto analysis over the full scenario set

The earlier smoke test compared SWAN to itself, so those ECDFs sat at 1.0 by construction. These new plots do **not** do that: Soroush and SWAN are different programs on the same inputs.

---

## How to re-run

From the `Soroush` folder, with the project virtual environment:

```bash
.\.venv\Scripts\python.exe progress_review_1\run_pr1_matched_baseline.py
```

Gurobi must be licensed (an academic WLS license is already in use on this machine). Expected runtime for this Review I slice is about 1–2 minutes after path computation.

---

## Files added or changed for this review

| Path | Role |
| --- | --- |
| `PROGRESS_REVIEW_1.md` | This explanation |
| `progress_review_1/run_pr1_matched_baseline.py` | Matched SWAN / GB / AW runner and plotter |
| `progress_review_1/pr1_matched_results.csv` | Numeric results |
| `progress_review_1/pr1_matched_results.json` | Same results plus timing breakdowns |
| `progress_review_1/figs/*.png` | Overlay plots for the review slides |
| `traffic_engineering/alg/geometric_approx_binning.py` | `gurobipy` import so Geometric Binner can run on Windows |
