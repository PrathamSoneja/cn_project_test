"""Milestone 2 reproducible Soroush-versus-SWAN evaluation.

This runner extends the matched Milestone 1 baseline to the three Soroush
allocators described in the paper: Geometric Binner (GB), Adaptive
Waterfiller (AW), and Equidepth Binner (EB).  Every method in a replicate
uses the same topology, traffic matrix, candidate paths and link capacity.

It deliberately writes one row per run before computing medians, so results
remain auditable rather than presenting a single best run as a conclusion.
"""
from __future__ import annotations

import argparse
import csv
csv.field_size_limit(2**31 - 1)
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
TE_DIR = ROOT / "traffic_engineering"
sys.path[:0] = [str(ROOT), str(TE_DIR)]


LINK_CAP = 1000.0
SWAN_ALPHA = 2.0
SWAN_U = 0.1
GB_MAX_EPSILON = 1e-6
FAIRNESS_THETA = 0.1


def total_flow(rate_map):
    return float(sum(np.sum(rates) for rates in rate_map.values()))


def parse_csv(value, cast=str):
    return [cast(item.strip()) for item in value.split(",") if item.strip()]


def choose_scenarios(topologies, models, scales, max_scenarios):
    """Select one deterministic matrix per topology/model/scale combination."""
    selected = []
    for topology in topologies:
        for model in models:
            by_scale = defaultdict(list)
            matrix_dir = ROOT / "ncflow" / "traffic-matrices" / model
            topology_file = ROOT / "ncflow" / "topologies" / "topology-zoo" / topology
            for matrix_file in matrix_dir.glob(f"{topology}*_traffic-matrix.pkl"):
                parts = matrix_file.stem.split("_")
                if len(parts) < 4:
                    continue
                try:
                    scale = float(parts[3])
                except ValueError:
                    continue
                by_scale[scale].append((topology, model, scale, str(topology_file), str(matrix_file)))
            for scale in scales:
                matches = sorted(by_scale.get(float(scale), []), key=lambda row: row[4])
                if matches:
                    selected.append(matches[0])
    if max_scenarios:
        selected = selected[:max_scenarios]
    if not selected:
        raise RuntimeError("No matrices matched the requested topology, model, and scale filters.")
    return selected


def make_paths(problem, num_paths, processors):
    from utilities import constants, shortest_paths

    return shortest_paths.all_pair_shortest_path_multi_processor(
        problem.G,
        problem.edge_idx,
        k=num_paths,
        number_processors=processors,
        base_split=0.9,
        split_type=constants.EXPONENTIAL_DECAY,
    )


def run_swan(problem, paths, _path_output, _args):
    from alg import swan_max_min_approx

    return swan_max_min_approx.max_min_approx(
        SWAN_ALPHA, SWAN_U, problem, paths, LINK_CAP, mcf_grb_method=2, break_down=False
    )


def run_gb(problem, paths, _path_output, args, alpha):
    from alg import geometric_approx_binning

    return geometric_approx_binning.max_min_approx(
        alpha,
        SWAN_U,
        problem,
        paths,
        LINK_CAP,
        max_epsilon=GB_MAX_EPSILON,
        mcf_grb_method=2,
    )


def run_aw(problem, _paths, path_output, args):
    from alg import approx_water_bet

    _, edge_map, path_len_map, _, split_map = path_output
    rates, duration, _satisfied, timing = approx_water_bet.get_rates(
        problem,
        split_map,
        args.num_paths,
        num_iter_approx_water=args.aw_water_iterations,
        num_iter_bet=args.aw_bet_iterations,
        link_cap=LINK_CAP,
        path_edge_idx_mapping=edge_map,
        path_path_len_mapping=path_len_map,
    )
    return rates, float(duration), timing


def run_eb(problem, paths, path_output, args, bins):
    from alg import approx_water_bet_plus_mcf

    _, edge_map, path_len_map, total_path_len_map, split_map = path_output
    rates, duration, timing = approx_water_bet_plus_mcf.get_rates(
        problem,
        paths,
        edge_map,
        path_len_map,
        total_path_len_map,
        split_map,
        args.num_paths,
        num_iter_approx_water=args.aw_water_iterations,
        num_iter_bet=args.aw_bet_iterations,
        link_cap=LINK_CAP,
        mcf_grb_method=2,
        num_bins=bins,
        min_beta=args.eb_min_beta,
        min_epsilon=args.eb_min_epsilon,
        k=1,
        alpha=0,
    )
    return rates, float(duration), timing


def method_specs(args):
    specs = [("SWAN", "baseline", run_swan)]
    specs.extend(("Geometric Binner", f"alpha={alpha:g}", lambda p, paths, po, a, alpha=alpha: run_gb(p, paths, po, a, alpha)) for alpha in args.gb_alphas)
    specs.append(("Adaptive Waterfiller", f"water={args.aw_water_iterations};bet={args.aw_bet_iterations}", run_aw))
    if args.include_eb:
        specs.extend(("Equidepth Binner", f"bins={bins}", lambda p, paths, po, a, bins=bins: run_eb(p, paths, po, a, bins)) for bins in args.eb_bins)
    return specs


def run_experiment(args, scenarios):
    from ncflow.lib.problem import Problem
    from scripts import benchmark_plot_utils
    from utilities import utils

    rows = []
    path_cache = {}
    specs = method_specs(args)
    for topology, model, scale, topology_file, matrix_file in scenarios:
        cache_key = (topology, args.num_paths)
        if cache_key not in path_cache:
            reference = Problem.from_file(topology_file, matrix_file)
            utils.revise_list_commodities(reference)
            print(f"Computing {args.num_paths} paths for {topology}")
            path_cache[cache_key] = make_paths(reference, args.num_paths, args.path_processors)
        path_output = path_cache[cache_key]
        paths = path_output[0]
        for repeat in range(1, args.repeats + 1):
            problem = Problem.from_file(topology_file, matrix_file)
            utils.revise_list_commodities(problem)
            print(f"{topology} {model} scale={scale:g} repeat={repeat}/{args.repeats}")
            baseline_rates, baseline_seconds, baseline_timing = run_swan(problem, paths, path_output, args)
            baseline_flow = total_flow(baseline_rates)
            for method, configuration, implementation in specs:
                if method == "SWAN":
                    rates, seconds, timing = baseline_rates, baseline_seconds, baseline_timing
                else:
                    rates, seconds, timing = implementation(problem, paths, path_output, args)
                flow = total_flow(rates)
                rows.append(
                    {
                        "topology": topology,
                        "traffic_model": model,
                        "scale_factor": float(scale),
                        "traffic_matrix": Path(matrix_file).name,
                        "num_demands": len(problem.sparse_commodity_list),
                        "num_paths": args.num_paths,
                        "repeat": repeat,
                        "method": method,
                        "configuration": configuration,
                        "runtime_s": float(seconds),
                        "total_flow": flow,
                        "relative_total_flow": flow / baseline_flow if baseline_flow else float("nan"),
                        "speedup_vs_swan": baseline_seconds / seconds if seconds else float("inf"),
                        "fairness_vs_swan": float(benchmark_plot_utils.compute_fairness_no(baseline_rates, rates, FAIRNESS_THETA)),
                        "timing_breakdown_json": json.dumps({key: float(value) for key, value in timing.items()}, sort_keys=True),
                    }
                )
    return rows


def summarize(rows):
    groups = defaultdict(list)
    keys = ("topology", "traffic_model", "scale_factor", "traffic_matrix", "num_demands", "num_paths", "method", "configuration")
    for row in rows:
        groups[tuple(row[key] for key in keys)].append(row)
    output = []
    for key, records in sorted(groups.items()):
        summary = dict(zip(keys, key))
        summary["repeats"] = len(records)
        for metric in ("runtime_s", "total_flow", "relative_total_flow", "speedup_vs_swan", "fairness_vs_swan"):
            values = [record[metric] for record in records]
            summary[f"median_{metric}"] = float(median(values))
            summary[f"min_{metric}"] = float(min(values))
            summary[f"max_{metric}"] = float(max(values))
        output.append(summary)
    return output


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def make_plots(summary, output_dir):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    methods = sorted({f"{row['method']} ({row['configuration']})" for row in summary if row["method"] != "SWAN"})
    colors = plt.cm.tab10(np.linspace(0, 1, max(len(methods), 1)))
    for metric, xlabel, stem, log_x in (
        ("median_speedup_vs_swan", "Speedup relative to SWAN", "speedup_ecdf", True),
        ("median_relative_total_flow", "Total flow relative to SWAN", "total_flow_ecdf", False),
        ("median_fairness_vs_swan", "Fairness relative to SWAN", "fairness_ecdf", False),
    ):
        plt.figure(figsize=(8.5, 5))
        for label, color in zip(methods, colors):
            records = [row for row in summary if f"{row['method']} ({row['configuration']})" == label]
            values = np.sort([row[metric] for row in records])
            plt.step(values, np.arange(1, len(values) + 1) / len(values), where="post", label=label, color=color, linewidth=2)
        plt.axvline(1.0, color="gray", linestyle=":", linewidth=1)
        if log_x:
            plt.xscale("log")
        plt.xlabel(xlabel)
        plt.ylabel("Fraction of matched scenarios")
        plt.legend(fontsize=8)
        plt.tight_layout()
        plt.savefig(output_dir / f"{stem}.png", dpi=170)
        plt.close()
    plt.figure(figsize=(8.5, 5.2))
    for label, color in zip(methods, colors):
        records = [row for row in summary if f"{row['method']} ({row['configuration']})" == label]
        plt.scatter([row["median_runtime_s"] for row in records], [row["median_fairness_vs_swan"] for row in records], label=label, color=color, s=55)
    plt.xscale("log")
    plt.xlabel("Median allocation runtime (seconds, log scale)")
    plt.ylabel("Fairness relative to SWAN")
    plt.legend(fontsize=8)
    plt.tight_layout()
    plt.savefig(output_dir / "runtime_fairness_pareto.png", dpi=170)
    plt.close()


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topologies", default="Uninett2010.graphml,Cogentco.graphml,GtsCe.graphml")
    parser.add_argument("--traffic-models", default="uniform,bimodal,gravity,poisson-high-inter")
    parser.add_argument("--scales", default="8,16,32,64")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--num-paths", type=int, default=16)
    parser.add_argument("--path-processors", type=int, default=8)
    parser.add_argument("--gb-alphas", default="1.25,1.5,2,4")
    parser.add_argument("--include-eb", action="store_true")
    parser.add_argument("--eb-bins", default="4,8,16")
    parser.add_argument("--eb-min-beta", type=float, default=1e-2)
    parser.add_argument("--eb-min-epsilon", type=float, default=1e-2)
    parser.add_argument("--aw-water-iterations", type=int, default=1)
    parser.add_argument("--aw-bet-iterations", type=int, default=10)
    parser.add_argument("--max-scenarios", type=int, default=0, help="Limit selected scenarios; 0 means all.")
    parser.add_argument("--plan", action="store_true", help="Print the resolved scenario plan without running solvers.")
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    args.topologies = parse_csv(args.topologies)
    args.traffic_models = parse_csv(args.traffic_models)
    args.scales = parse_csv(args.scales, float)
    args.gb_alphas = parse_csv(args.gb_alphas, float)
    args.eb_bins = parse_csv(args.eb_bins, int)
    if args.repeats < 1 or args.num_paths < 1:
        parser.error("--repeats and --num-paths must be positive")
    return args


def main():
    args = arguments()
    scenarios = choose_scenarios(args.topologies, args.traffic_models, args.scales, args.max_scenarios)
    print(f"Resolved {len(scenarios)} matched scenarios")
    for topology, model, scale, _, matrix in scenarios:
        print(f"  {topology} | {model} | scale={scale:g} | {Path(matrix).name}")
    if args.plan:
        return
    output_dir = args.output_dir or ROOT / "progress_review_2" / "runs" / datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=False)
    rows = run_experiment(args, scenarios)
    summary = summarize(rows)
    write_csv(output_dir / "per_run_results.csv", rows)
    write_csv(output_dir / "median_summary.csv", summary)
    (output_dir / "experiment_config.json").write_text(json.dumps(vars(args), default=str, indent=2), encoding="utf-8")
    make_plots(summary, output_dir)
    print(f"Wrote reproducible Milestone 2 results to {output_dir}")


if __name__ == "__main__":
    main()
