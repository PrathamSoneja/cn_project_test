"""Sweep the elephant-flow and elephant-capacity fractions on one TE case."""
from __future__ import annotations

import argparse
import csv
csv.field_size_limit(2**31 - 1)
import json
from datetime import datetime
from pathlib import Path

import numpy as np

try:
    from .hybrid_hybrid_te import (
        Experiment,
        ExperimentInput,
        LINK_CAPACITY,
        MovingAverage,
        SCENARIOS,
        _cached_paths,
        choose_scenarios,
        generate_dynamic_workload,
    )
except ImportError:
    from hybrid_hybrid_te import (
        Experiment,
        ExperimentInput,
        LINK_CAPACITY,
        MovingAverage,
        SCENARIOS,
        _cached_paths,
        choose_scenarios,
        generate_dynamic_workload,
    )

def values(text):
    return [float(item.strip()) for item in text.split(",") if item.strip()]


def run(args):
    from ncflow.lib.problem import Problem
    from utilities import utils

    selected = choose_scenarios(
        (args.topology,), (args.traffic_model,), (args.scale,), max_scenarios=1
    )
    topology, model, scale, topology_file, matrix_file = selected[0]
    problem = Problem.from_file(str(topology_file), str(matrix_file))
    utils.revise_list_commodities(problem)
    cache_dir = args.cache_dir or Path(__file__).resolve().parent / ".path-cache"
    path_output = _cached_paths(problem, topology, args.num_paths, args.path_processors, cache_dir)
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
    inputs = ExperimentInput(
        graph=problem.G,
        path_output=path_output,
        capacities=np.full(len(problem.G.edges()), LINK_CAPACITY),
        demand_matrices=matrices,
    )
    forecaster = MovingAverage(matrices[0], window=8)
    forecasts = []
    for demand in matrices:
        forecasts.append(forecaster.predict())
        forecaster.observe(demand)

    output = args.output or Path(__file__).resolve().parent / "grid_runs" / datetime.now().strftime("%Y%m%d_%H%M%S")
    output.mkdir(parents=True, exist_ok=False)
    per_epoch_path = output / "per_epoch.csv"
    summary_path = output / "summary.csv"
    rows = []
    print(f"Case: {topology} | {model} | scale={scale:g} | {args.scenario} | epochs={args.epochs}")
    for elephant_fraction in args.elephant_fractions:
        for capacity_fraction in args.capacity_fractions:
            if not 0 < elephant_fraction <= 1 or not 0 < capacity_fraction <= 1:
                raise ValueError("sweep fractions must be in (0, 1]")
            print(f"Testing flow_fraction={elephant_fraction:g}, capacity_fraction={capacity_fraction:g}")
            experiment = Experiment(
                inputs,
                elephant_fraction=elephant_fraction,
                elephant_capacity_fraction=capacity_fraction,
            )
            trial = experiment.run_scenario(
                matrices,
                forecasts,
                controller_workers=1,
                methods=("periodic_gb", "hybrid_ml_elephant_mice"),
            )
            total_demand = np.sum(matrices, axis=(1, 2))
            for row in trial:
                row.update(
                    {
                        "topology": topology,
                        "traffic_model": model,
                        "scale_factor": scale,
                        "scenario": args.scenario,
                        "seed": args.seed,
                        "elephant_fraction": elephant_fraction,
                        "elephant_capacity_fraction": capacity_fraction,
                        "unmet_fraction": row["unmet_demand"] / max(float(total_demand[row["epoch"]]), 1e-9),
                    }
                )
                rows.append(row)

    columns = list(rows[0])
    with per_epoch_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    grouped = {}
    keys = ("elephant_fraction", "elephant_capacity_fraction", "method")
    for row in rows:
        key = tuple(row[column] for column in keys)
        grouped.setdefault(key, []).append(row)
    summary_rows = []
    for key, records in sorted(grouped.items()):
        item = dict(zip(keys, key))
        item["epochs"] = len(records)
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
            "gb_reruns",
            "capacity_violations",
        ):
            item[f"median_{metric}"] = float(np.median([record[metric] for record in records]))
        summary_rows.append(item)
    periodic = {
        (row["elephant_fraction"], row["elephant_capacity_fraction"]): row
        for row in summary_rows
        if row["method"] == "periodic_gb"
    }
    for row in summary_rows:
        reference = periodic[(row["elephant_fraction"], row["elephant_capacity_fraction"])]
        if row["method"] == "hybrid_ml_elephant_mice":
            row["served_ratio_vs_periodic_gb"] = row["median_served_demand"] / max(reference["median_served_demand"], 1e-9)
            row["unmet_delta_vs_periodic_gb"] = row["median_unmet_demand"] - reference["median_unmet_demand"]
            row["runtime_ratio_vs_periodic_gb"] = row["median_runtime_s"] / max(reference["median_runtime_s"], 1e-9)
        else:
            row["served_ratio_vs_periodic_gb"] = 1.0
            row["unmet_delta_vs_periodic_gb"] = 0.0
            row["runtime_ratio_vs_periodic_gb"] = 1.0
    with summary_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary_rows[0]))
        writer.writeheader()
        writer.writerows(summary_rows)
    (output / "grid_config.json").write_text(
        json.dumps(
            {
                "topology": topology,
                "traffic_model": model,
                "scale": scale,
                "scenario": args.scenario,
                "seed": args.seed,
                "epochs": args.epochs,
                "num_paths": args.num_paths,
                "elephant_fractions": args.elephant_fractions,
                "capacity_fractions": args.capacity_fractions,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"Saved raw epochs: {per_epoch_path}")
    print(f"Saved parameter comparison: {summary_path}")
    return summary_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology", default="Uninett2010.graphml")
    parser.add_argument("--traffic-model", default="uniform")
    parser.add_argument("--scale", type=float, default=8.0)
    parser.add_argument("--scenario", choices=SCENARIOS, default="sudden_spike")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--epochs", type=int, default=15)
    parser.add_argument("--num-paths", type=int, default=16)
    parser.add_argument("--path-processors", type=int, default=8)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--elephant-fractions", type=values, default=values("0.10,0.15,0.20"))
    parser.add_argument("--capacity-fractions", type=values, default=values("0.70,0.85,0.95"))
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if args.epochs < 9:
        parser.error("--epochs must be at least 9 to include the generated demand shift")
    if args.num_paths < 1 or args.path_processors < 1:
        parser.error("--num-paths and --path-processors must be positive")
    run(args)


if __name__ == "__main__":
    main()
