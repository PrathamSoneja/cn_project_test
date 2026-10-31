"""Run resumable, matched controller comparisons across the Uninett grid."""
from __future__ import annotations

import argparse
import csv
csv.field_size_limit(2**31 - 1)
import hashlib
import json
import os
import pickle
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

if __package__:
    from . import compare_hybrid_baselines as comparison
    from .hybrid_hybrid_te import (
        ALL_SCENARIOS,
        DEFAULT_EPOCHS,
        LINK_CAPACITY,
        METHOD_NAMES,
        NORMAL_SCENARIO,
        SOROUSH,
        TRAFFIC_MODELS,
        Experiment,
        ExperimentInput,
        MovingAverage,
        _cached_paths,
        choose_scenarios,
        generate_dynamic_workload,
    )
else:
    import compare_hybrid_baselines as comparison
    from hybrid_hybrid_te import (
        ALL_SCENARIOS,
        DEFAULT_EPOCHS,
        LINK_CAPACITY,
        METHOD_NAMES,
        NORMAL_SCENARIO,
        SOROUSH,
        TRAFFIC_MODELS,
        Experiment,
        ExperimentInput,
        MovingAverage,
        _cached_paths,
        choose_scenarios,
        generate_dynamic_workload,
    )

TOPOLOGY = "Uninett2010.graphml"
ALL_METHODS = (*METHOD_NAMES, *comparison.BASELINE_METHODS)
RESULT_COLUMNS = comparison.RESULT_COLUMNS
RUNTIME_COLUMNS = (
    "scenario_id",
    "topology",
    "traffic_model",
    "scale_factor",
    "scenario",
    "epoch",
    "method",
    "runtime_benchmark_s",
    "runtime_repeats",
)


class _ProgressBar:
    def __init__(self, label, total, completed=0):
        self.label = label
        self.total = max(1, total)
        self.completed = min(completed, self.total)
        self.started_completed = self.completed
        self.started = time.monotonic()
        self._render()

    def _render(self):
        fraction = min(1.0, self.completed / self.total)
        width = 28
        filled = int(width * fraction)
        elapsed = time.monotonic() - self.started
        completed_this_run = self.completed - self.started_completed
        remaining = (
            elapsed / completed_this_run * (self.total - self.completed)
            if completed_this_run > 0
            else 0.0
        )
        bar = "#" * filled + "-" * (width - filled)
        print(
            f"{self.label} [{bar}] {self.completed}/{self.total} "
            f"({fraction:.1%}) | elapsed {elapsed:.0f}s | ETA {remaining:.0f}s",
            end="\n" if self.completed >= self.total else "\r",
            file=sys.stderr,
            flush=True,
        )

    def update(self, completed=None):
        self.completed = (
            min(self.total, self.completed + 1)
            if completed is None
            else min(self.total, completed)
        )
        self._render()


def _write_rows(path: Path, rows: list[dict]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _read_rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(RESULT_COLUMNS):
            raise ValueError(f"{path} does not have the expected results columns")
        return list(reader)


def _append_rows(path: Path, rows: list[dict], columns: tuple[str, ...]) -> None:
    needs_header = not path.is_file() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        if needs_header:
            writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def _replace_rows(path: Path, rows: list[dict], columns: tuple[str, ...]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _write_checkpoint(path: Path, checkpoint: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        pickle.dump(checkpoint, handle, protocol=pickle.HIGHEST_PROTOCOL)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _forecasts(matrices):
    forecaster = MovingAverage(matrices[0], window=8)
    forecasts = []
    for demand in matrices:
        forecasts.append(forecaster.predict())
        forecaster.observe(demand)
    return forecasts


def _make_row(metadata, epoch, method, metrics, demand, runtime, repeats):
    row = {
        **metadata,
        "epoch": epoch,
        "method": method,
        "method_description": comparison.METHOD_DESCRIPTIONS[method],
        **metrics,
        "unmet_fraction": metrics["unmet_demand"]
        / max(float(np.sum(demand)), 1e-9),
        "runtime_s": runtime,
        "runtime_repeats": repeats,
    }
    return row


def _summary(rows):
    grouped = {}
    group_columns = (
        "topology",
        "traffic_model",
        "scale_factor",
        "scenario",
        "method",
    )
    metrics = (
        "served_demand",
        "unmet_demand",
        "unmet_fraction",
        "jain_index",
        "min_served_fraction",
        "p05_served_fraction",
        "fairness_vs_periodic_gb",
        "runtime_s",
        "allocation_changes",
    )
    for row in rows:
        key = (
            row["topology"],
            row["traffic_model"],
            float(row["scale_factor"]),
            row["scenario"],
            row["method"],
        )
        grouped.setdefault(key, []).append(row)
    summaries = []
    for key, records in sorted(grouped.items()):
        summary = dict(zip(group_columns, key))
        summary["method_description"] = records[0]["method_description"]
        summary["epochs"] = len(records)
        for metric in metrics:
            summary[f"median_{metric}"] = statistics.median(
                float(row[metric]) for row in records
            )
        summary["capacity_violations"] = sum(
            int(row["capacity_violations"]) for row in records
        )
        summary["gb_reruns"] = sum(int(row["gb_reruns"]) for row in records)
        summaries.append(summary)
    return summaries


def _save_reports(output: Path, rows: list[dict]) -> None:
    summary_rows = _summary(rows)
    benchmark_path = output / "runtime_benchmarks.csv"
    benchmark_values = {}
    if benchmark_path.is_file():
        with benchmark_path.open("r", encoding="utf-8", newline="") as handle:
            for record in csv.DictReader(handle):
                key = (
                    record["traffic_model"],
                    float(record["scale_factor"]),
                    record["scenario"],
                    record["method"],
                )
                benchmark_values.setdefault(key, []).append(
                    float(record["runtime_benchmark_s"])
                )
    for record in summary_rows:
        key = (
            record["traffic_model"],
            float(record["scale_factor"]),
            record["scenario"],
            record["method"],
        )
        values = benchmark_values.get(key, [])
        record["median_runtime_benchmark_s"] = (
            statistics.median(values) if values else ""
        )
    summary_path = output / "summary.csv"
    if summary_rows:
        with summary_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(summary_rows[0]))
            writer.writeheader()
            writer.writerows(summary_rows)
    if rows:
        try:
            from .analyze_results import create_figures, load_results, summarize_epochs
        except ImportError:
            from analyze_results import create_figures, load_results, summarize_epochs

        per_epoch_path = output / "per_epoch.csv"
        frame = load_results(per_epoch_path)
        summarize_epochs(frame).to_csv(output / "median_summary.csv", index=False)
        for plot_path in create_figures(frame, output):
            print(f"Saved graph: {plot_path}", flush=True)


def run(args):
    if args.topology != TOPOLOGY:
        raise ValueError(f"This full-grid runner is specific to {TOPOLOGY}")
    if args.controller_workers != 1:
        raise ValueError("the matched sweep requires --controller-workers 1")
    if args.epochs < 1:
        raise ValueError("--epochs must be positive")
    if args.num_paths < 1 or args.path_processors < 1 or args.runtime_repeats < 0:
        raise ValueError(
            "path counts and processor count must be positive; runtime repeats must be non-negative"
        )

    scenarios = tuple(args.scenarios)
    if not scenarios or any(scenario not in ALL_SCENARIOS for scenario in scenarios):
        raise ValueError(f"scenarios must be selected from {ALL_SCENARIOS}")
    if args.epochs < 9 and any(
        scenario != NORMAL_SCENARIO for scenario in scenarios
    ):
        raise ValueError(
            "--epochs must be at least 9 when running generated shift scenarios"
        )
    selected = choose_scenarios(
        (TOPOLOGY,), tuple(args.traffic_models), tuple(args.scales), 0
    )
    matrix_cases = []
    rng = np.random.default_rng(args.seed)
    for topology, model, scale, topology_file, selected_matrix in selected:
        matching_matrices = []
        model_dir = SOROUSH / "ncflow" / "traffic-matrices" / model
        for matrix_file in model_dir.glob(f"{TOPOLOGY}*_traffic-matrix.pkl"):
            parts = matrix_file.stem.split("_")
            if len(parts) > 3 and float(parts[3]) == float(scale):
                matching_matrices.append(matrix_file)
        matching_matrices.sort()
        if args.matrix_selection == "first":
            matching_matrices = matching_matrices[:1]
        elif args.matrix_selection == "sample":
            if args.matrices_per_stratum > len(matching_matrices):
                raise ValueError(
                    f"Requested {args.matrices_per_stratum} matrices for "
                    f"{model} at scale {scale:g}, but only "
                    f"{len(matching_matrices)} are available"
                )
            chosen = rng.choice(
                len(matching_matrices),
                size=args.matrices_per_stratum,
                replace=False,
            )
            matching_matrices = [
                matching_matrices[int(index)] for index in sorted(chosen)
            ]
        matrix_cases.extend(
            (topology, model, scale, topology_file, matrix_file)
            for matrix_file in matching_matrices
        )
    cases = [
        {
            "topology": topology,
            "traffic_model": model,
            "scale_factor": float(scale),
            "topology_file": str(topology_file),
            "matrix_file": str(matrix_file),
            "scenario": scenario,
        }
        for topology, model, scale, topology_file, matrix_file in matrix_cases
        for scenario in scenarios
    ]
    if args.max_cases:
        cases = cases[: args.max_cases]
    if not cases:
        raise ValueError("The selected Uninett case grid is empty")
    print(
        f"Selected {len(cases)} cases, {args.epochs} epochs/case, "
        f"{len(ALL_METHODS)} methods, {args.runtime_repeats} separate timing repeat(s)",
        flush=True,
    )

    output = args.output or (
        Path(__file__).resolve().parent
        / "uninett_runs"
        / datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    output = Path(output)
    output.mkdir(parents=True, exist_ok=args.resume)
    results_path = output / "per_epoch.csv"
    checkpoint_path = output / "checkpoint.pkl"
    runtime_path = output / "runtime_benchmarks.csv"
    config_path = output / "run_config.json"
    run_config = {
        "topology": TOPOLOGY,
        "traffic_models": list(args.traffic_models),
        "scales": [float(scale) for scale in args.scales],
        "matrix_selection": args.matrix_selection,
        "matrices_per_stratum": args.matrices_per_stratum,
        "scenarios": list(scenarios),
        "epochs": args.epochs,
        "seed": args.seed,
        "num_paths": args.num_paths,
        "path_processors": args.path_processors,
        "controller_workers": 1,
        "runtime_repeats": args.runtime_repeats,
        "link_capacity": LINK_CAPACITY,
        "methods": list(ALL_METHODS),
        "cases": cases,
        "max_cases": args.max_cases,
        "objective": (
            "All nine methods use the same generated epoch demands, candidate "
            "paths, and uniform link capacities. Fairness is measured against "
            "periodic GB on actual demand."
        ),
        "timing": (
            "All algorithms run serially with one controller worker. "
            + (
                "runtime_benchmarks.csv stores medians from separate serial timing repeats."
                if args.runtime_repeats
                else "runtime_benchmarks.csv reuses per-controller serial timings from the quality pass."
            )
        ),
        "baseline_note": (
            "B4 and B4+ are the repository implementations of the established "
            "WAN baseline; no claim is made that their settings are learned."
        ),
    }
    fingerprint = hashlib.sha256(
        json.dumps(run_config, sort_keys=True).encode("utf-8")
    ).hexdigest()

    if args.resume:
        if not checkpoint_path.is_file() or not results_path.is_file():
            raise FileNotFoundError(
                "Resume requires checkpoint.pkl and per_epoch.csv in the output directory"
            )
        with checkpoint_path.open("rb") as handle:
            checkpoint = pickle.load(handle)
        if checkpoint.get("fingerprint") != fingerprint:
            raise ValueError("Resume settings differ from the saved run configuration")
        rows = _read_rows(results_path)
        row_count = int(checkpoint["row_count"])
        if len(rows) < row_count:
            raise ValueError(
                "The per-epoch CSV has fewer rows than the latest checkpoint"
            )
        rows = rows[:row_count]
        _write_rows(results_path, rows)
        case_index = checkpoint["case_index"]
        epoch_index = checkpoint["epoch_index"]
        phase = checkpoint["phase"]
        case_rows = checkpoint["case_rows"]
        case_state = checkpoint["case_state"]
        runtime_samples = checkpoint["runtime_samples"]
        runtime_repeat_index = checkpoint["runtime_repeat_index"]
        baseline_runtime_index = checkpoint.get("baseline_runtime_index", 0)
        if case_index < len(cases):
            print(
                f"Resuming case {case_index + 1}/{len(cases)}, epoch "
                f"{epoch_index + 1}/{args.epochs}, phase={phase}",
                flush=True,
            )
        else:
            print("All cases are complete; regenerating summary and plots.", flush=True)
    else:
        if any(output.iterdir()):
            raise FileExistsError(
                f"Output directory is not empty: {output}. Use --resume or choose another path."
            )
        config_path.write_text(json.dumps(run_config, indent=2), encoding="utf-8")
        rows = []
        case_index = 0
        epoch_index = 0
        phase = "quality"
        case_rows = []
        case_state = None
        runtime_samples = {}
        runtime_repeat_index = 0
        baseline_runtime_index = 0
        checkpoint = {}
        _write_rows(results_path, rows)

    cache_dir = args.cache_dir or Path(__file__).resolve().parent / ".path-cache"
    path_cache = {}
    from ncflow.lib.problem import Problem
    from utilities import utils

    def save_checkpoint():
        _write_checkpoint(
            checkpoint_path,
            {
                "fingerprint": fingerprint,
                "case_index": case_index,
                "epoch_index": epoch_index,
                "phase": phase,
                "row_count": len(rows),
                "case_rows": case_rows,
                "case_state": case_state,
                "runtime_samples": runtime_samples,
                "runtime_repeat_index": runtime_repeat_index,
                "baseline_runtime_index": baseline_runtime_index,
            },
        )

    if not args.resume:
        save_checkpoint()

    quality_total = len(cases) * args.epochs
    quality_completed = (
        case_index * args.epochs + epoch_index
        if phase == "quality"
        else (case_index + 1) * args.epochs
    )
    quality_progress = _ProgressBar(
        "Quality", quality_total, quality_completed
    )
    runtime_per_case = max(
        1,
        args.epochs
        * args.runtime_repeats
        * (1 + len(comparison.BASELINE_METHODS)),
    )
    runtime_total = len(cases) * runtime_per_case
    runtime_completed = case_index * runtime_per_case
    if phase == "runtime":
        runtime_completed += (
            runtime_repeat_index * args.epochs + baseline_runtime_index
        )
    runtime_progress = (
        _ProgressBar("Runtime benchmark", runtime_total, runtime_completed)
        if args.runtime_repeats
        else None
    )

    while case_index < len(cases):
        case = cases[case_index]
        topology, model, scale = (
            case["topology"],
            case["traffic_model"],
            case["scale_factor"],
        )
        topology_file = Path(case["topology_file"])
        matrix_file = Path(case["matrix_file"])
        problem = Problem.from_file(str(topology_file), str(matrix_file))
        utils.revise_list_commodities(problem)
        if topology not in path_cache:
            path_cache[topology] = _cached_paths(
                problem, topology, args.num_paths, args.path_processors, cache_dir
            )
        path_output = path_cache[topology]
        case_seed = args.seed + case_index
        matrices = generate_dynamic_workload(
            case_seed,
            args.epochs,
            case["scenario"],
            problem.traffic_matrix.tm,
            path_output[0],
            problem.G,
            scale=scale,
            traffic_model=model,
        )
        inputs = ExperimentInput(
            graph=problem.G,
            path_output=path_output,
            capacities=np.full(len(problem.G.edges()), LINK_CAPACITY, dtype=float),
            demand_matrices=matrices,
        )
        forecasts = _forecasts(matrices)
        scenario_id = (
            f"{topology}|{model}|{scale:g}|{matrix_file.stem}|{case['scenario']}"
        )
        metadata = {
            "topology": topology,
            "traffic_model": model,
            "scale_factor": scale,
            "traffic_matrix": matrix_file.name,
            "scenario": case["scenario"],
            "scenario_id": scenario_id,
            "seed": case_seed,
        }

        if phase == "quality":
            experiment = Experiment(inputs)
            for epoch in range(epoch_index, args.epochs):
                latest_state = []

                def capture_state(_epoch, _epoch_rows, state):
                    latest_state.append(state)

                controller_rows = experiment.run_scenario(
                    matrices[: epoch + 1],
                    forecasts[: epoch + 1],
                    methods=METHOD_NAMES,
                    controller_workers=1,
                    start_epoch=epoch,
                    initial_state=case_state,
                    on_checkpoint=capture_state,
                )
                current_controller_rows = [
                    row for row in controller_rows if row["epoch"] == epoch
                ]
                reference = experiment._capacity_safe(
                    experiment.allocator.gb(matrices[epoch], experiment.capacity),
                    matrices[epoch],
                    experiment.capacity,
                )
                completed_rows = []
                baseline_previous = (
                    case_state.get(
                        "baseline_previous",
                        {method: {} for method in comparison.BASELINE_METHODS},
                    )
                    if case_state is not None
                    else {method: {} for method in comparison.BASELINE_METHODS}
                )
                for source in current_controller_rows:
                    metric_values = {
                        key: source[key]
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
                    completed_rows.append(
                        _make_row(
                            metadata,
                            epoch,
                            source["method"],
                            metric_values,
                            matrices[epoch],
                            source["runtime_s"],
                            args.runtime_repeats,
                        )
                    )
                with open(os.devnull, "w", encoding="utf-8") as null:
                    with comparison.contextlib.redirect_stdout(null):
                        for method in comparison.BASELINE_METHODS:
                            allocation, duration = comparison._b4_allocation(
                                method,
                                inputs.graph,
                                matrices[epoch],
                                path_output,
                                experiment.capacity,
                            )
                            metric_values = comparison._baseline_metrics(
                                experiment,
                                allocation,
                                reference,
                                matrices[epoch],
                                baseline_previous[method],
                            )
                            baseline_previous[method] = comparison._copy_allocation(
                                allocation
                            )
                            completed_rows.append(
                                _make_row(
                                    metadata,
                                    epoch,
                                    method,
                                    metric_values,
                                    matrices[epoch],
                                    duration,
                                    args.runtime_repeats,
                                )
                            )
                case_rows.extend(completed_rows)
                rows.extend(completed_rows)
                case_state = latest_state[0]
                case_state["baseline_previous"] = baseline_previous
                epoch_index = epoch + 1
                _append_rows(results_path, completed_rows, RESULT_COLUMNS)
                save_checkpoint()
                quality_progress.update(case_index * args.epochs + epoch + 1)
                print(
                    f"Saved {case['scenario']} | {model} | scale={scale:g} "
                    f"epoch {epoch + 1}/{args.epochs} "
                    f"({case_index + 1}/{len(cases)} cases)",
                    flush=True,
                )

            phase = "runtime"
            runtime_samples = {
                (method, epoch): []
                for method in ALL_METHODS
                for epoch in range(args.epochs)
            }
            runtime_repeat_index = 0
            save_checkpoint()

        if phase == "runtime":
            for repeat in range(runtime_repeat_index, args.runtime_repeats):
                def report_runtime_epoch(epoch, _epoch_rows):
                    if runtime_progress is not None:
                        runtime_progress.update(
                            case_index * runtime_per_case
                            + repeat * args.epochs
                            + epoch
                            + 1
                        )

                trial = Experiment(inputs).run_scenario(
                    matrices,
                    forecasts,
                    methods=METHOD_NAMES,
                    controller_workers=1,
                    on_epoch_complete=report_runtime_epoch,
                )
                for row in trial:
                    runtime_samples[(row["method"], row["epoch"])].append(
                        row["runtime_s"]
                    )
                runtime_repeat_index = repeat + 1
                save_checkpoint()
                print(
                    f"Saved controller runtime repeat "
                    f"{runtime_repeat_index}/{args.runtime_repeats} for "
                    f"{case['scenario']} | {model} | scale={scale:g}",
                    flush=True,
                )

            baseline_runtime_tasks = [
                (method, epoch, repeat)
                for method in comparison.BASELINE_METHODS
                for epoch in range(args.epochs)
                for repeat in range(args.runtime_repeats)
            ]
            baseline_index = baseline_runtime_index
            with open(os.devnull, "w", encoding="utf-8") as null, comparison.contextlib.redirect_stdout(null):
                for task_index in range(baseline_index, len(baseline_runtime_tasks)):
                    method, epoch, _repeat = baseline_runtime_tasks[task_index]
                    _allocation, duration = comparison._b4_allocation(
                        method,
                        inputs.graph,
                        matrices[epoch],
                        path_output,
                        Experiment(inputs).capacity,
                    )
                    runtime_samples[(method, epoch)].append(duration)
                    baseline_runtime_index = task_index + 1
                    save_checkpoint()
                    if runtime_progress is not None:
                        runtime_progress.update(
                            case_index * runtime_per_case
                            + runtime_repeat_index * args.epochs
                            + baseline_runtime_index
                        )

            existing_runtime_rows = []
            if runtime_path.is_file():
                with runtime_path.open("r", encoding="utf-8", newline="") as handle:
                    existing_runtime_rows = list(csv.DictReader(handle))
            expected_runtime_keys = {
                (int(row["epoch"]), row["method"]) for row in case_rows
            }
            current_runtime_rows = [
                row
                for row in existing_runtime_rows
                if row.get("scenario_id") == scenario_id
            ]
            observed_runtime_keys = {
                (int(row["epoch"]), row["method"])
                for row in current_runtime_rows
                if row.get("epoch") is not None and row.get("method") is not None
            }
            if (
                observed_runtime_keys != expected_runtime_keys
                or len(current_runtime_rows) != len(expected_runtime_keys)
            ):
                existing_runtime_rows = [
                    row
                    for row in existing_runtime_rows
                    if row.get("scenario_id") != scenario_id
                ]
                benchmark_rows = []
                for row in case_rows:
                    benchmark_rows.append(
                        {
                            "scenario_id": row["scenario_id"],
                            "topology": row["topology"],
                            "traffic_model": row["traffic_model"],
                            "scale_factor": row["scale_factor"],
                            "scenario": row["scenario"],
                            "epoch": row["epoch"],
                            "method": row["method"],
                            "runtime_benchmark_s": statistics.median(
                                runtime_samples[(row["method"], row["epoch"])]
                                or [float(row["runtime_s"])]
                            ),
                            "runtime_repeats": args.runtime_repeats,
                        }
                    )
                existing_runtime_rows.extend(benchmark_rows)
                _replace_rows(runtime_path, existing_runtime_rows, RUNTIME_COLUMNS)
            case_index += 1
            epoch_index = 0
            phase = "quality"
            case_rows = []
            case_state = None
            runtime_samples = {}
            runtime_repeat_index = 0
            baseline_runtime_index = 0
            checkpoint = {}
            save_checkpoint()

    _save_reports(output, rows)
    quality_progress.update(quality_total)
    if runtime_progress is not None:
        runtime_progress.update(runtime_total)
    print(f"Completed {len(cases)} Uninett cases / {len(rows)} method-epoch rows.")
    print(f"Per-epoch results: {results_path}")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology", default=TOPOLOGY, choices=(TOPOLOGY,))
    parser.add_argument(
        "--traffic-models", default=",".join(TRAFFIC_MODELS),
        help="Comma-separated traffic models (default: all four)",
    )
    parser.add_argument(
        "--scales",
        default="32",
        help="Comma-separated load scales (default: 32 for the focused suite)",
    )
    parser.add_argument(
        "--scenarios", default=",".join(ALL_SCENARIOS),
        help="Comma-separated scenarios (default: normal and all dynamic shifts)",
    )
    parser.add_argument(
        "--matrix-selection",
        choices=("sample", "all", "first"),
        default="sample",
        help="Use a seeded sample, all matrices, or the first sorted matrix per stratum",
    )
    parser.add_argument(
        "--matrices-per-stratum",
        type=int,
        default=1,
        help="Matrices per traffic-model/scale stratum for sample selection",
    )
    parser.add_argument("--epochs", type=int, default=DEFAULT_EPOCHS)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--num-paths",
        type=int,
        default=3,
        help="Candidate paths per demand (default: 3 for the focused screening suite)",
    )
    parser.add_argument("--path-processors", type=int, default=8)
    parser.add_argument("--controller-workers", type=int, default=1)
    parser.add_argument(
        "--runtime-repeats",
        type=int,
        default=0,
        help="Separate serial timing repetitions (0 uses serial quality-pass timings)",
    )
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    args.traffic_models = tuple(
        value.strip() for value in args.traffic_models.split(",") if value.strip()
    )
    args.scales = tuple(
        float(value.strip()) for value in args.scales.split(",") if value.strip()
    )
    args.scenarios = tuple(
        value.strip() for value in args.scenarios.split(",") if value.strip()
    )
    if args.epochs < 9 and any(
        scenario != NORMAL_SCENARIO for scenario in args.scenarios
    ):
        parser.error("--epochs must be at least 9 for generated shift scenarios")
    if args.epochs < 1 or args.num_paths < 1 or args.path_processors < 1:
        parser.error("--epochs and path settings must be positive")
    if args.controller_workers != 1 or args.runtime_repeats < 0:
        parser.error("--controller-workers must be 1 and --runtime-repeats non-negative")
    if args.max_cases < 0 or args.matrices_per_stratum < 1:
        parser.error("--max-cases cannot be negative and --matrices-per-stratum must be positive")
    run(args)


if __name__ == "__main__":
    main()
