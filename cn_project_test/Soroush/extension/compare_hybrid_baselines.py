"""Compare Hybrid Adaptive with matched traffic-engineering baselines."""
from __future__ import annotations

import argparse
import contextlib
import csv
csv.field_size_limit(2**31 - 1)
import io
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from time import perf_counter

import numpy as np

try:
    from .hybrid_hybrid_te import (
        LINK_CAPACITY,
        Experiment,
        ExperimentInput,
        METHOD_NAMES,
        MovingAverage,
        ALL_SCENARIOS,
        SCENARIOS,
        _cached_paths,
        _churn,
        _copy_allocation,
        choose_scenarios,
        generate_dynamic_workload,
    )
except ImportError:
    from hybrid_hybrid_te import (
        LINK_CAPACITY,
        Experiment,
        ExperimentInput,
        METHOD_NAMES,
        MovingAverage,
        ALL_SCENARIOS,
        SCENARIOS,
        _cached_paths,
        _churn,
        _copy_allocation,
        choose_scenarios,
        generate_dynamic_workload,
    )

CONTROLLER_METHODS = METHOD_NAMES
BASELINE_METHODS = {
    "b4": "B4 max-min fair (repository implementation)",
    "b4_plus": "B4+ max-min fair (repository implementation)",
}
METHOD_DESCRIPTIONS = {
    "hybrid_basic": "Hybrid Basic",
    "hybrid_fixed": "Hybrid Fixed",
    "hybrid_adaptive": "Hybrid Adaptive",
    "hybrid_ml_elephant_mice": "Hybrid ML Elephant/Mice",
    "gb_only": "Forecast GB",
    "periodic_gb": "Periodic GB",
    "aw_only": "AW",
    **BASELINE_METHODS,
}
RESULT_COLUMNS = (
    "topology",
    "traffic_model",
    "scale_factor",
    "traffic_matrix",
    "scenario",
    "scenario_id",
    "seed",
    "epoch",
    "method",
    "method_description",
    "served_demand",
    "unmet_demand",
    "unmet_fraction",
    "per_flow_metrics_json",
    "jain_index",
    "min_served_fraction",
    "p05_served_fraction",
    "fairness_vs_periodic_gb",
    "capacity_violations",
    "allocation_changes",
    "gb_reruns",
    "runtime_s",
    "runtime_repeats",
)


def _b4_allocation(method, graph, demand, path_output, capacity):
    from ncflow.lib.problem import Problem
    from alg import b4_experience_w_global_software_wan as b4
    from alg import b4_extended

    setup_started = perf_counter()
    problem = Problem(graph, traffic_matrix=demand)
    setup_runtime = perf_counter() - setup_started
    function = b4.get_rates if method == "b4" else b4_extended.get_rates
    allocation, duration, _timings = function(
        problem,
        max(len(paths) for paths in path_output[0].values()),
        float(capacity),
        path_edge_idx_mapping=path_output[1],
        path_path_len_mapping=path_output[2],
    )
    return allocation, setup_runtime + float(duration)


def _baseline_metrics(
    experiment, allocation, reference, demand, previous_allocation=None
):
    loads = experiment.link_load(allocation)
    unmet = experiment._unmet(demand, allocation)
    return {
        "served_demand": experiment._served(allocation),
        "unmet_demand": unmet,
        "unmet_fraction": unmet / max(float(np.sum(demand)), 1e-9),
        **experiment._flow_metrics(demand, allocation),
        "fairness_vs_periodic_gb": experiment._fairness(reference, allocation),
        "capacity_violations": int(
            np.count_nonzero(loads > experiment.capacity + 1e-6)
        ),
        "allocation_changes": _churn(previous_allocation or {}, allocation),
        "gb_reruns": 0,
    }


def _runtime_samples(inputs, matrices, forecasts, repeats):
    samples = defaultdict(list)
    for _ in range(repeats):
        trial = Experiment(inputs).run_scenario(
            matrices,
            forecasts,
            methods=CONTROLLER_METHODS,
            controller_workers=1,
        )
        for row in trial:
            samples[(row["method"], row["epoch"])].append(row["runtime_s"])

    experiment = Experiment(inputs)
    baseline_functions = {
        "b4": "b4",
        "b4_plus": "b4_plus",
    }
    with contextlib.redirect_stdout(io.StringIO()):
        for method in baseline_functions:
            for epoch, demand in enumerate(matrices):
                epoch_samples = []
                for _ in range(repeats):
                    _allocation, duration = _b4_allocation(
                        baseline_functions[method],
                        inputs.graph,
                        demand,
                        inputs.path_output,
                        experiment.capacity,
                    )
                    epoch_samples.append(duration)
                samples[(method, epoch)].extend(epoch_samples)
    return samples


def run(args):
    from ncflow.lib.problem import Problem
    from utilities import utils

    if args.controller_workers != 1:
        raise ValueError("the matched comparison requires --controller-workers 1")
    if args.epochs < 9:
        raise ValueError("--epochs must be at least 9 to include the generated demand shift")
    if args.runtime_repeats < 1:
        raise ValueError("--runtime-repeats must be positive")

    selected = choose_scenarios(
        (args.topology,), (args.traffic_model,), (args.scale,), max_scenarios=1
    )
    topology, model, scale, topology_file, matrix_file = selected[0]
    problem = Problem.from_file(str(topology_file), str(matrix_file))
    utils.revise_list_commodities(problem)
    cache_dir = args.cache_dir or Path(__file__).resolve().parent / ".path-cache"
    path_output = _cached_paths(
        problem, topology, args.num_paths, args.path_processors, cache_dir
    )
    matrices = generate_dynamic_workload(
        args.seed,
        args.epochs,
        args.scenario,
        problem.traffic_matrix.tm,
        path_output[0],
        problem.G,
        scale=scale,
        traffic_model=model,
    )
    capacities = np.full(len(problem.G.edges()), LINK_CAPACITY, dtype=float)
    inputs = ExperimentInput(
        graph=problem.G,
        path_output=path_output,
        capacities=capacities,
        demand_matrices=matrices,
    )
    forecaster = MovingAverage(matrices[0], window=8)
    forecasts = []
    for demand in matrices:
        forecasts.append(forecaster.predict())
        forecaster.observe(demand)

    experiment = Experiment(inputs)
    controller_rows = experiment.run_scenario(
        matrices,
        forecasts,
        methods=CONTROLLER_METHODS,
        controller_workers=1,
    )
    metrics = {
        (row["method"], row["epoch"]): {
            key: row[key]
            for key in (
                "served_demand",
                "unmet_demand",
                "per_flow_metrics_json",
                "jain_index",
                "min_served_fraction",
                "p05_served_fraction",
                "fairness_vs_periodic_gb",
                "capacity_violations",
                "allocation_changes",
                "gb_reruns",
            )
        }
        for row in controller_rows
    }

    # Evaluate both repository B4 implementations against the exact periodic-GB
    # allocation for each epoch, using the same candidate routes and link limit.
    with contextlib.redirect_stdout(io.StringIO()):
        previous_baselines = {method: {} for method in BASELINE_METHODS}
        for epoch, demand in enumerate(matrices):
            reference = experiment._capacity_safe(
                experiment.allocator.gb(demand, experiment.capacity),
                demand,
                experiment.capacity,
            )
            for method in BASELINE_METHODS:
                allocation, _duration = _b4_allocation(
                    method, inputs.graph, demand, path_output, experiment.capacity
                )
                metrics[(method, epoch)] = _baseline_metrics(
                    experiment,
                    allocation,
                    reference,
                    demand,
                    previous_baselines[method],
                )
                previous_baselines[method] = _copy_allocation(allocation)

    samples = _runtime_samples(
        inputs, matrices, forecasts, args.runtime_repeats
    )
    output = args.output or (
        Path(__file__).resolve().parent
        / "comparison_runs"
        / datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    output.mkdir(parents=True, exist_ok=False)
    scenario_id = f"{topology}|{model}|{scale:g}|{args.scenario}"
    rows = []
    all_methods = (*CONTROLLER_METHODS, *BASELINE_METHODS)
    for method in all_methods:
        for epoch, demand in enumerate(matrices):
            row = {
                "topology": topology,
                "traffic_model": model,
                "scale_factor": scale,
                "traffic_matrix": matrix_file.name,
                "scenario": args.scenario,
                "scenario_id": scenario_id,
                "seed": args.seed,
                "epoch": epoch,
                "method": method,
                "method_description": METHOD_DESCRIPTIONS[method],
                **metrics[(method, epoch)],
                "runtime_s": statistics.median(samples[(method, epoch)]),
                "runtime_repeats": args.runtime_repeats,
            }
            row["unmet_fraction"] = row["unmet_demand"] / max(
                float(np.sum(demand)), 1e-9
            )
            rows.append(row)

    per_epoch_path = output / "per_epoch.csv"
    with per_epoch_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    summary_rows = []
    for method in all_methods:
        method_rows = [row for row in rows if row["method"] == method]
        summary = {
            "method": method,
            "method_description": METHOD_DESCRIPTIONS[method],
            "epochs": len(method_rows),
        }
        for metric in (
            "served_demand",
            "unmet_demand",
            "unmet_fraction",
            "jain_index",
            "min_served_fraction",
            "p05_served_fraction",
            "fairness_vs_periodic_gb",
            "runtime_s",
            "allocation_changes",
        ):
            summary[f"median_{metric}"] = statistics.median(
                row[metric] for row in method_rows
            )
        summary["capacity_violations"] = sum(
            row["capacity_violations"] for row in method_rows
        )
        summary["gb_reruns"] = sum(row["gb_reruns"] for row in method_rows)
        summary_rows.append(summary)
    summary_path = output / "summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary_rows[0]))
        writer.writeheader()
        writer.writerows(summary_rows)

    config = {
        "topology": topology,
        "traffic_model": model,
        "scale": scale,
        "traffic_matrix": str(matrix_file),
        "scenario": args.scenario,
        "scenario_id": scenario_id,
        "seed": args.seed,
        "epochs": args.epochs,
        "num_paths": args.num_paths,
        "path_processors": args.path_processors,
        "link_capacity": LINK_CAPACITY,
        "controller_workers": 1,
        "runtime_repeats": args.runtime_repeats,
        "methods": list(all_methods),
        "objective": (
            "All allocations use the same epoch demands, candidate paths, and "
            "uniform link capacity; compare served/unmet demand and fairness "
            "against periodic GB on actual demand."
        ),
        "runtime_method": (
            "Median of separately repeated serial per-epoch controller runs; "
            "quality metrics are from one common epoch sequence."
        ),
    }
    (output / "comparison_config.json").write_text(
        json.dumps(config, indent=2), encoding="utf-8"
    )
    print(f"Saved per-epoch matched results: {per_epoch_path}")
    print(f"Saved median summary: {summary_path}")
    print("B4 and B4+ rows use the repository implementations without tuned settings.")
    return summary_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology", default="Uninett2010.graphml")
    parser.add_argument("--traffic-model", default="uniform")
    parser.add_argument("--scale", type=float, default=8.0)
    parser.add_argument("--scenario", choices=ALL_SCENARIOS, default="sudden_spike")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--epochs", type=int, default=15)
    parser.add_argument("--num-paths", type=int, default=16)
    parser.add_argument("--path-processors", type=int, default=8)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--controller-workers", type=int, default=1)
    parser.add_argument("--runtime-repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if args.num_paths < 1 or args.path_processors < 1:
        parser.error("--num-paths and --path-processors must be positive")
    if args.controller_workers != 1:
        parser.error("matched comparisons require --controller-workers 1")
    if args.epochs < 9 or args.runtime_repeats < 1:
        parser.error("--epochs must be at least 9 and --runtime-repeats positive")
    run(args)


if __name__ == "__main__":
    main()
