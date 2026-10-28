"""Create publication-style plots from hybrid TE results."""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


METRICS = {
    "avg_unmet_demand": "unmet_demand",
    "avg_fairness": "fairness_vs_periodic_gb",
    "avg_runtime": "runtime_s",
}
EPOCH_SUMMARY_METRICS = (
    "served_demand",
    "unmet_demand",
    "fairness_vs_periodic_gb",
    "capacity_violations",
    "runtime_s",
    "allocation_changes",
    "gb_reruns",
)


def load_results(csv_path: Path) -> pd.DataFrame:
    """Load aggregates or summarize the extension's per-epoch CSV."""
    frame = pd.read_csv(csv_path)
    required = {"scenario", "method"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")

    if "fairness_vs_periodic_gb" not in frame and "fairness_vs_gb_actual" in frame:
        frame["fairness_vs_periodic_gb"] = frame["fairness_vs_gb_actual"]

    for metric, raw_column in METRICS.items():
        if metric not in frame.columns:
            if raw_column not in frame.columns:
                raise ValueError(
                    f"Expected '{metric}' or its per-epoch column '{raw_column}'"
                )
            frame[metric] = pd.to_numeric(frame[raw_column], errors="raise")
        else:
            frame[metric] = pd.to_numeric(frame[metric], errors="raise")

    frame = frame.dropna(subset=["scenario", "method"])
    if frame.empty:
        raise ValueError("No result rows were found in the CSV")
    return frame


def summarize_epochs(frame: pd.DataFrame) -> pd.DataFrame:
    """Aggregate per-epoch measurements as medians per case."""
    group_columns = [
        column
        for column in (
            "scenario_id",
            "topology",
            "traffic_model",
            "scale_factor",
            "scenario",
            "method",
        )
        if column in frame.columns
    ]
    summary_frame = frame.copy()
    for average_column, raw_column in METRICS.items():
        if raw_column not in summary_frame and average_column in summary_frame:
            summary_frame[raw_column] = summary_frame[average_column]
    metric_columns = [
        column for column in EPOCH_SUMMARY_METRICS if column in summary_frame.columns
    ]
    if not group_columns or not metric_columns:
        raise ValueError("Results need scenario/method identifiers and epoch metrics")

    summary = (
        summary_frame.groupby(group_columns, dropna=False)[metric_columns]
        .median()
        .add_prefix("median_")
        .reset_index()
    )
    if "epoch" in summary_frame.columns:
        epoch_counts = summary_frame.groupby(group_columns, dropna=False)["epoch"].nunique()
        summary = summary.merge(
            epoch_counts.rename("epoch_count").reset_index(),
            on=group_columns,
            how="left",
        )
    return summary


def _save_grouped_bar(
    frame, metric, category, title, ylabel, output_path, *, ylim=None, figsize=(11, 6)
):
    fig, ax = plt.subplots(figsize=figsize, constrained_layout=True)
    values = frame.pivot_table(
        index=category, columns="method", values=metric, aggfunc="mean"
    )
    scenarios = list(values.index)
    methods = list(values.columns)
    positions = range(len(scenarios))
    width = 0.8 / max(len(methods), 1)
    colors = plt.get_cmap("tab10")
    for index, method in enumerate(methods):
        offsets = [position + (index - (len(methods) - 1) / 2) * width for position in positions]
        ax.bar(offsets, values[method].to_numpy(), width, label=method, color=colors(index % 10))
    ax.set(title=title, xlabel=category.replace("_", " ").title(), ylabel=ylabel)
    ax.set_xticks(list(positions), [str(value) for value in scenarios])
    if ylim is not None:
        ax.set_ylim(*ylim)
    ax.tick_params(axis="x", rotation=15)
    ax.legend(
        title="Method",
        bbox_to_anchor=(1.02, 1),
        loc="upper left",
        borderaxespad=0,
        frameon=False,
    )
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def create_figures(frame: pd.DataFrame, output_dir: Path) -> list[Path]:
    """Write unmet-demand, fairness, and runtime charts."""
    output_dir.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update(
        {
            "axes.titleweight": "bold",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "figure.dpi": 120,
        }
    )
    paths = [
        output_dir / "unmet_demand_by_scenario.png",
        output_dir / "fairness_by_scenario.png",
        output_dir / "runtime_by_topology.png",
    ]
    scenario_frame = frame
    if "scale_factor" in frame.columns:
        saturated = frame.loc[pd.to_numeric(frame["scale_factor"], errors="coerce") == 64.0]
        if not saturated.empty:
            scenario_frame = saturated
    _save_grouped_bar(
        scenario_frame,
        "avg_unmet_demand",
        "scenario",
        "Unmet demand by scenario and controller",
        "Average unmet demand",
        paths[0],
    )
    _save_grouped_bar(
        frame,
        "avg_fairness",
        "scenario",
        "Fairness fidelity by scenario and controller",
        "Fairness vs. periodic GB on actual demand",
        paths[1],
        ylim=(0.0, 1.05),
    )

    runtime_frame = frame.copy()
    if "topology" not in runtime_frame.columns:
        runtime_frame["topology"] = "all_topologies"
    runtime = runtime_frame.groupby(
        ["topology", "method"], as_index=False
    )["avg_runtime"].median()
    if not (runtime["avg_runtime"] > 0).any():
        raise ValueError("Runtime values must include at least one positive value for log scale")
    runtime["plot_runtime"] = runtime["avg_runtime"].clip(
        lower=float(runtime.loc[runtime["avg_runtime"] > 0, "avg_runtime"].min()) * 0.5
    )
    values = runtime.pivot(index="topology", columns="method", values="plot_runtime")
    fig, ax = plt.subplots(figsize=(10, 5.8), constrained_layout=True)
    values.plot(kind="bar", ax=ax, colormap="tab10")
    ax.set_yscale("log")
    ax.set(
        title="Median allocation runtime by topology and controller",
        xlabel="Topology",
        ylabel="Median runtime per epoch (seconds, log scale)",
    )
    ax.tick_params(axis="x", rotation=15)
    ax.legend(title="Method", bbox_to_anchor=(1.02, 1), loc="upper left", frameon=False)
    fig.savefig(paths[2], dpi=300, bbox_inches="tight")
    plt.close(fig)
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).resolve().parent / "results.csv",
        help="Input results CSV (default: results.csv beside this script)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory (default: same directory as the input CSV)",
    )
    args = parser.parse_args()
    if not args.input.is_file():
        parser.error(f"Input CSV does not exist: {args.input}")

    frame = load_results(args.input)
    output_dir = args.output_dir or args.input.resolve().parent
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "median_summary.csv"
    summarize_epochs(frame).to_csv(summary_path, index=False)
    print(f"Saved median epoch summary to {summary_path}")
    for path in create_figures(frame, output_dir):
        print(f"Saved {path}")


if __name__ == "__main__":
    main()
