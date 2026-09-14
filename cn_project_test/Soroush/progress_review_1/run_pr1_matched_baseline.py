"""
Progress Review I matched baseline.

Runs SWAN and two Soroush allocators on the same Uninett2010 + uniform
traffic matrices, 16 candidate shortest paths, and the same link capacity.
Writes a CSV, overlay plots, and a JSON summary used by the write-up.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from collections import defaultdict
from multiprocessing import freeze_support
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
TE_DIR = ROOT / "traffic_engineering"
sys.path.insert(0, str(TE_DIR))
sys.path.insert(0, str(ROOT))

from alg import approx_water_bet, geometric_approx_binning, swan_max_min_approx
from ncflow.lib.problem import Problem
from scripts import benchmark_plot_utils
from utilities import constants, shortest_paths, utils

TOPO_NAME = "Uninett2010.graphml"
TM_MODEL = "uniform"
NUM_PATHS = 16
LINK_CAP = 1000.0
ALPHA = 2
U = 0.1
MAX_EPSILON = 1e-6
MCF_GRB_METHOD = 2
NUM_PATH_PROCESSORS = 8
NUM_ITER_APPROX = 1
NUM_ITER_BET = 10
THETA_FAIRNESS = 0.1
TARGET_SCALES = (8.0, 16.0, 32.0, 64.0)
OUT_DIR = Path(__file__).resolve().parent
FIG_DIR = OUT_DIR / "figs"
LOG_DIR = OUT_DIR / "logs"


def total_allocated(rate_map) -> float:
    return float(sum(np.sum(rates) for rates in rate_map.values()))


def demand_rate_map(rate_map):
    return {key: float(np.sum(rates)) for key, rates in rate_map.items()}


def pick_matched_scenarios():
    fnames = utils.find_topo_tm_fname(TOPO_NAME, TM_MODEL)
    by_scale = defaultdict(list)
    for item in fnames:
        by_scale[float(item[2])].append(item)
    selected = []
    for scale in TARGET_SCALES:
        if scale not in by_scale:
            continue
        chosen = sorted(by_scale[scale], key=lambda row: row[4])[0]
        selected.append(chosen)
    if not selected:
        raise RuntimeError(
            f"No Uninett2010/{TM_MODEL} traffic matrices found for scales {TARGET_SCALES}"
        )
    return selected


def compute_paths(problem):
    return shortest_paths.all_pair_shortest_path_multi_processor(
        problem.G,
        problem.edge_idx,
        k=NUM_PATHS,
        number_processors=NUM_PATH_PROCESSORS,
        base_split=0.9,
        split_type=constants.EXPONENTIAL_DECAY,
    )


def run_swan(problem, path_dict):
    mapping, duration, run_time_dict = swan_max_min_approx.max_min_approx(
        ALPHA,
        U,
        problem,
        path_dict,
        LINK_CAP,
        mcf_grb_method=MCF_GRB_METHOD,
        break_down=False,
    )
    return mapping, float(duration), run_time_dict


def run_geometric_binner(problem, path_dict):
    mapping, duration, run_time_dict = geometric_approx_binning.max_min_approx(
        ALPHA,
        U,
        problem,
        path_dict,
        LINK_CAP,
        max_epsilon=MAX_EPSILON,
        mcf_grb_method=MCF_GRB_METHOD,
    )
    return mapping, float(duration), run_time_dict


def run_adaptive_waterfiller(problem, path_output):
    (
        path_dict,
        path_edge_idx_mapping,
        path_path_len_mapping,
        _path_total_path_len_mapping,
        path_split_ratio_mapping,
    ) = path_output
    mapping, duration, _satisfied, run_time_dict = approx_water_bet.get_rates(
        problem,
        path_split_ratio_mapping,
        NUM_PATHS,
        num_iter_approx_water=NUM_ITER_APPROX,
        num_iter_bet=NUM_ITER_BET,
        link_cap=LINK_CAP,
        path_edge_idx_mapping=path_edge_idx_mapping,
        path_path_len_mapping=path_path_len_mapping,
    )
    return mapping, float(duration), run_time_dict


def save_pickle(obj, folder: Path, name: str):
    path = folder / name
    utils.write_to_file_as_pickle(obj, str(folder), str(path))
    return str(path)


def ecdf_xy(values):
    data = np.sort(np.asarray(values, dtype=float))
    y = np.arange(1, data.size + 1) / data.size
    return data, y


def make_plots(rows):
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    methods = ["Geometric Binner (Soroush)", "Adaptive Waterfiller (Soroush)"]
    colors = {
        "Geometric Binner (Soroush)": "#d62728",
        "Adaptive Waterfiller (Soroush)": "#1f77b4",
        "SWAN": "#8c564b",
    }

    plt.figure(figsize=(8.5, 5))
    for method in methods:
        values = [row["relative_total_flow"][method] for row in rows]
        x, y = ecdf_xy(values)
        plt.step(x, y, where="post", label=method, color=colors[method], linewidth=2.4)
    plt.axvline(1.0, color="gray", linestyle=":", linewidth=1.2, label="Same as SWAN")
    plt.xlabel("Total allocated traffic relative to SWAN")
    plt.ylabel("Fraction of matched scenarios")
    plt.title("Uninett2010, uniform traffic, 16 shortest paths")
    plt.legend(fontsize=9)
    plt.tight_layout()
    flow_path = FIG_DIR / "pr1_total_flow_relative_to_swan.png"
    plt.savefig(flow_path, dpi=160)
    plt.close()

    plt.figure(figsize=(8.5, 5))
    for method in methods:
        values = [row["speedup"][method] for row in rows]
        x, y = ecdf_xy(values)
        plt.step(x, y, where="post", label=method, color=colors[method], linewidth=2.4)
    plt.axvline(1.0, color="gray", linestyle=":", linewidth=1.2, label="Same speed as SWAN")
    plt.xscale("log")
    plt.xlabel("Speedup relative to SWAN (log scale)")
    plt.ylabel("Fraction of matched scenarios")
    plt.title("Uninett2010, uniform traffic, 16 shortest paths")
    plt.legend(fontsize=9)
    plt.tight_layout()
    speed_path = FIG_DIR / "pr1_speedup_relative_to_swan.png"
    plt.savefig(speed_path, dpi=160)
    plt.close()

    plt.figure(figsize=(8.5, 5.2))
    for method, marker in [
        ("SWAN", "o"),
        ("Geometric Binner (Soroush)", "s"),
        ("Adaptive Waterfiller (Soroush)", "^"),
    ]:
        xs = [row["runtime_s"][method] for row in rows]
        ys = [row["total_flow"][method] for row in rows]
        plt.scatter(xs, ys, label=method, color=colors[method], marker=marker, s=70)
    plt.xlabel("End-to-end allocation runtime (seconds)")
    plt.ylabel("Total allocated traffic")
    plt.title("Matched overlay: runtime vs total flow")
    plt.legend(fontsize=9)
    plt.tight_layout()
    pareto_path = FIG_DIR / "pr1_runtime_vs_total_flow.png"
    plt.savefig(pareto_path, dpi=160)
    plt.close()

    plt.figure(figsize=(8.5, 5))
    for method in methods:
        values = [row["fairness_vs_swan"][method] for row in rows]
        x, y = ecdf_xy(values)
        plt.step(x, y, where="post", label=method, color=colors[method], linewidth=2.4)
    plt.xlabel("Fairness number relative to SWAN (1.0 is a perfect match)")
    plt.ylabel("Fraction of matched scenarios")
    plt.title("Allocation quality vs SWAN on matched instances")
    plt.legend(fontsize=9)
    plt.tight_layout()
    fair_path = FIG_DIR / "pr1_fairness_relative_to_swan.png"
    plt.savefig(fair_path, dpi=160)
    plt.close()

    return {
        "total_flow_plot": str(flow_path),
        "speedup_plot": str(speed_path),
        "pareto_plot": str(pareto_path),
        "fairness_plot": str(fair_path),
    }


def write_csv(rows, path: Path):
    fieldnames = [
        "topology",
        "traffic_model",
        "scale_factor",
        "traffic_matrix",
        "num_paths",
        "num_demands",
        "swan_total_flow",
        "swan_runtime_s",
        "gb_total_flow",
        "gb_runtime_s",
        "gb_relative_total_flow",
        "gb_speedup",
        "gb_fairness_vs_swan",
        "aw_total_flow",
        "aw_runtime_s",
        "aw_relative_total_flow",
        "aw_speedup",
        "aw_fairness_vs_swan",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "topology": row["topology"],
                    "traffic_model": row["traffic_model"],
                    "scale_factor": row["scale_factor"],
                    "traffic_matrix": row["traffic_matrix"],
                    "num_paths": row["num_paths"],
                    "num_demands": row["num_demands"],
                    "swan_total_flow": row["total_flow"]["SWAN"],
                    "swan_runtime_s": row["runtime_s"]["SWAN"],
                    "gb_total_flow": row["total_flow"]["Geometric Binner (Soroush)"],
                    "gb_runtime_s": row["runtime_s"]["Geometric Binner (Soroush)"],
                    "gb_relative_total_flow": row["relative_total_flow"]["Geometric Binner (Soroush)"],
                    "gb_speedup": row["speedup"]["Geometric Binner (Soroush)"],
                    "gb_fairness_vs_swan": row["fairness_vs_swan"]["Geometric Binner (Soroush)"],
                    "aw_total_flow": row["total_flow"]["Adaptive Waterfiller (Soroush)"],
                    "aw_runtime_s": row["runtime_s"]["Adaptive Waterfiller (Soroush)"],
                    "aw_relative_total_flow": row["relative_total_flow"]["Adaptive Waterfiller (Soroush)"],
                    "aw_speedup": row["speedup"]["Adaptive Waterfiller (Soroush)"],
                    "aw_fairness_vs_swan": row["fairness_vs_swan"]["Adaptive Waterfiller (Soroush)"],
                }
            )


def main():
    freeze_support()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)

    scenarios = pick_matched_scenarios()
    print(f"Selected {len(scenarios)} matched scenarios")
    for item in scenarios:
        print(" ", item[2], os.path.basename(item[4]))

    example = scenarios[0]
    problem0 = Problem.from_file(example[3], example[4])
    print("Computing 16 shortest paths on Uninett2010...")
    path_output = compute_paths(problem0)
    path_dict = path_output[0]

    rows = []
    for topo_name, tm_model, scale_factor, topo_path, tm_path in scenarios:
        problem = Problem.from_file(topo_path, tm_path)
        utils.revise_list_commodities(problem)
        tm_base = os.path.basename(tm_path)
        print("=" * 72)
        print(f"Running matched instance: {tm_base} scale={scale_factor}")

        swan_map, swan_dur, swan_rt = run_swan(problem, path_dict)
        gb_map, gb_dur, gb_rt = run_geometric_binner(problem, path_dict)
        aw_map, aw_dur, aw_rt = run_adaptive_waterfiller(problem, path_output)

        swan_flow = total_allocated(swan_map)
        gb_flow = total_allocated(gb_map)
        aw_flow = total_allocated(aw_map)
        gb_fair = float(benchmark_plot_utils.compute_fairness_no(swan_map, gb_map, THETA_FAIRNESS))
        aw_fair = float(benchmark_plot_utils.compute_fairness_no(swan_map, aw_map, THETA_FAIRNESS))

        tag = Path(tm_base).stem + f"_num_paths_{NUM_PATHS}"
        save_pickle(swan_map, LOG_DIR, f"swan_{tag}_per_path_flow.pickle")
        save_pickle(gb_map, LOG_DIR, f"gb_{tag}_per_path_flow.pickle")
        save_pickle(aw_map, LOG_DIR, f"aw_{tag}_per_path_flow.pickle")
        save_pickle(swan_rt, LOG_DIR, f"swan_{tag}_run_time_dict.pickle")
        save_pickle(gb_rt, LOG_DIR, f"gb_{tag}_run_time_dict.pickle")
        save_pickle(aw_rt, LOG_DIR, f"aw_{tag}_run_time_dict.pickle")

        row = {
            "topology": topo_name,
            "traffic_model": tm_model,
            "scale_factor": float(scale_factor),
            "traffic_matrix": tm_base,
            "num_paths": NUM_PATHS,
            "num_demands": len(problem.sparse_commodity_list),
            "total_flow": {
                "SWAN": swan_flow,
                "Geometric Binner (Soroush)": gb_flow,
                "Adaptive Waterfiller (Soroush)": aw_flow,
            },
            "runtime_s": {
                "SWAN": swan_dur,
                "Geometric Binner (Soroush)": gb_dur,
                "Adaptive Waterfiller (Soroush)": aw_dur,
            },
            "relative_total_flow": {
                "Geometric Binner (Soroush)": gb_flow / swan_flow,
                "Adaptive Waterfiller (Soroush)": aw_flow / swan_flow,
            },
            "speedup": {
                "Geometric Binner (Soroush)": swan_dur / gb_dur,
                "Adaptive Waterfiller (Soroush)": swan_dur / aw_dur,
            },
            "fairness_vs_swan": {
                "Geometric Binner (Soroush)": gb_fair,
                "Adaptive Waterfiller (Soroush)": aw_fair,
            },
            "run_time_breakdown": {
                "SWAN": {k: float(v) for k, v in swan_rt.items()},
                "Geometric Binner (Soroush)": {k: float(v) for k, v in gb_rt.items()},
                "Adaptive Waterfiller (Soroush)": {k: float(v) for k, v in aw_rt.items()},
            },
        }
        rows.append(row)
        print(
            f"  SWAN flow={swan_flow:.2f} time={swan_dur:.3f}s | "
            f"GB flow={gb_flow:.2f} time={gb_dur:.3f}s speedup={row['speedup']['Geometric Binner (Soroush)']:.2f} | "
            f"AW flow={aw_flow:.2f} time={aw_dur:.3f}s speedup={row['speedup']['Adaptive Waterfiller (Soroush)']:.2f}"
        )

    csv_path = OUT_DIR / "pr1_matched_results.csv"
    json_path = OUT_DIR / "pr1_matched_results.json"
    write_csv(rows, csv_path)
    plot_paths = make_plots(rows)
    summary = {
        "milestone": "Progress Review I",
        "topology": TOPO_NAME,
        "traffic_model": TM_MODEL,
        "num_paths": NUM_PATHS,
        "link_cap": LINK_CAP,
        "matched_knobs": [
            "same topology",
            "same traffic matrix",
            "same scale factor",
            "same 16 shortest-path set",
            "same link capacity",
        ],
        "num_scenarios": len(rows),
        "plots": plot_paths,
        "csv": str(csv_path),
        "rows": rows,
        "median_gb_speedup": float(np.median([r["speedup"]["Geometric Binner (Soroush)"] for r in rows])),
        "median_aw_speedup": float(np.median([r["speedup"]["Adaptive Waterfiller (Soroush)"] for r in rows])),
        "median_gb_rel_flow": float(np.median([r["relative_total_flow"]["Geometric Binner (Soroush)"] for r in rows])),
        "median_aw_rel_flow": float(np.median([r["relative_total_flow"]["Adaptive Waterfiller (Soroush)"] for r in rows])),
        "median_gb_fairness": float(np.median([r["fairness_vs_swan"]["Geometric Binner (Soroush)"] for r in rows])),
        "median_aw_fairness": float(np.median([r["fairness_vs_swan"]["Adaptive Waterfiller (Soroush)"] for r in rows])),
    }
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("Wrote", csv_path)
    print("Wrote", json_path)
    for label, path in plot_paths.items():
        print("Wrote", label, path)


if __name__ == "__main__":
    main()
