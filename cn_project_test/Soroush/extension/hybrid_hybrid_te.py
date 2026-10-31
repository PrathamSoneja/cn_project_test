"""Hybrid proactive/reactive traffic engineering experiments."""
from __future__ import annotations

import argparse
import csv
csv.field_size_limit(2**31 - 1)
import hashlib
import json
import os
import pickle
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

if __package__:
    from .classifier import OnlineElephantMiceClassifier
else:
    from classifier import OnlineElephantMiceClassifier

SOROUSH = Path(__file__).resolve().parents[1]
TE_DIR = SOROUSH / "traffic_engineering"
sys.path[:0] = [str(SOROUSH), str(TE_DIR)]

METHOD_NAMES = (
    "gb_only",
    "aw_only",
    "periodic_gb",
    "hybrid_basic",
    "hybrid_fixed",
    "hybrid_adaptive",
    "hybrid_ml_elephant_mice",
)
TOPOLOGIES = ("Uninett2010.graphml", "GtsCe.graphml", "Cogentco.graphml")
TRAFFIC_MODELS = ("uniform", "bimodal", "gravity", "poisson-high-inter")
LOAD_SCALES = (8.0, 16.0, 32.0, 64.0)
SCENARIOS = ("sudden_spike", "hotspot_shift", "dcn_incast", "demand_drop")
NORMAL_SCENARIO = "normal"
ALL_SCENARIOS = (NORMAL_SCENARIO,) + SCENARIOS
FAIRNESS_FLOOR = 0.1
LINK_CAPACITY = 1000.0
DEFAULT_EPOCHS = 15
DEFAULT_CONTROLLER_WORKERS = 2

RESULT_COLUMNS = (
    "scenario",
    "scenario_id",
    "topology",
    "traffic_model",
    "scale_factor",
    "traffic_matrix",
    "num_demands",
    "num_paths",
    "epoch",
    "method",
    "served_demand",
    "unmet_demand",
    "per_flow_metrics_json",
    "jain_index",
    "min_served_fraction",
    "p05_served_fraction",
    "fairness_vs_periodic_gb",
    "capacity_violations",
    "runtime_s",
    "allocation_changes",
    "gb_reruns",
)


def _format_duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class _ProgressReporter:
    def __init__(self, total_scenarios, epochs_per_scenario, completed_epochs=0):
        self.total_scenarios = total_scenarios
        self.epochs_per_scenario = epochs_per_scenario
        self.total_epochs = total_scenarios * epochs_per_scenario
        self.completed_epochs = completed_epochs
        self._updates_this_run = 0
        self.started = time.monotonic()

    def update(self, scenario_index, scenario_id, epoch):
        self.completed_epochs += 1
        self._updates_this_run += 1
        elapsed = time.monotonic() - self.started
        remaining = elapsed / self._updates_this_run * (
            self.total_epochs - self.completed_epochs
        )
        completed_scenarios = scenario_index + int(
            epoch + 1 == self.epochs_per_scenario
        )
        bar_width = 24
        filled = int(bar_width * self.completed_epochs / self.total_epochs)
        bar = "#" * filled + "-" * (bar_width - filled)
        print(
            f"Progress [{bar}] {self.completed_epochs}/{self.total_epochs} epochs | "
            f"scenarios {completed_scenarios}/{self.total_scenarios} | "
            f"scenario {scenario_index + 1}/{self.total_scenarios} "
            f"epoch {epoch + 1}/{self.epochs_per_scenario} ({scenario_id}) | "
            f"elapsed {_format_duration(elapsed)} | "
            f"ETA {_format_duration(remaining)}",
            flush=True,
        )


@dataclass
class ExperimentInput:
    """Network inputs in the same graph, path-output and dense-TM formats as TE."""

    graph: object
    path_output: tuple
    capacities: np.ndarray
    demand_matrices: np.ndarray


class Allocator:
    """Thin adapters for the existing TE allocators."""

    def __init__(self, graph, path_output, capacity):
        from alg import approx_water_bet, geometric_approx_binning
        from ncflow.lib.problem import Problem

        self.graph = graph
        self.path_output = path_output
        self.paths = path_output[0]
        self.capacity = float(capacity)
        self.problem_type = Problem
        self.gb_fn = geometric_approx_binning.max_min_approx
        self.aw_fn = approx_water_bet.get_rates

    def _problem(self, demand):
        return self.problem_type(self.graph, traffic_matrix=np.asarray(demand, dtype=float))

    def gb(self, demand, capacity):
        if not np.any(np.asarray(demand) > 1e-9):
            return {}
        allocation = self.gb_fn(
            2.0, 0.1, self._problem(demand), self.paths, float(capacity),
            max_epsilon=1e-6, mcf_grb_method=2,
        )[0]
        normalized = {}
        for pair, bins in allocation.items():
            routes = self.paths.get(pair, ())
            if not routes:
                continue
            split = np.asarray(self.path_output[4].get(pair, ()), dtype=float)[:len(routes)]
            if split.size != len(routes) or np.sum(split) <= 0:
                split = np.full(len(routes), 1.0 / len(routes))
            else:
                split = split / np.sum(split)
            normalized[pair] = float(np.sum(bins)) * split
        return normalized

    def aw(self, demand, capacity):
        if capacity <= 1e-9 or not np.any(demand > 1e-9):
            return {}
        rates, _duration, _satisfied, _timing = self.aw_fn(
            self._problem(demand),
            self.path_output[4],
            len(next(iter(self.paths.values()))),
            num_iter_approx_water=1,
            num_iter_bet=5,
            link_cap=float(capacity),
            path_edge_idx_mapping=self.path_output[1],
            path_path_len_mapping=self.path_output[2],
        )
        normalized = {}
        for pair, values in rates.items():
            path_count = len(self.paths.get(pair, ()))
            if path_count:
                normalized[pair] = np.asarray(values, dtype=float)[:path_count].copy()
        return normalized


def _copy_allocation(allocation):
    return {key: np.asarray(value, dtype=float).copy() for key, value in allocation.items()}


class Controller:
    """Common controller interface with a per-epoch forecast setter."""

    def __init__(self, experiment, method):
        self.experiment = experiment
        self.method = method
        self.forecast = None

    def set_forecast(self, forecast):
        self.forecast = np.asarray(forecast, dtype=float)

    def run(self, epoch_demand, ml_prediction=None):
        if self.forecast is None:
            raise RuntimeError("set_forecast must be called before run")
        return self.experiment.allocate(
            self.method, epoch_demand, self.forecast, ml_prediction
        )


class Experiment:
    """Run all controller variants over a sequence of traffic matrices."""

    def __init__(
        self, inputs, *, q=0.9, window=8, hmax=0.25, fixed_headroom=0.1,
        elephant_fraction=0.15, elephant_capacity_fraction=0.85,
    ):
        self.inputs = inputs
        capacities = np.asarray(inputs.capacities, dtype=float).reshape(-1)
        edge_count = len(inputs.graph.edges())
        if (
            capacities.size == 0
            or capacities.size != edge_count
            or np.any(~np.isfinite(capacities))
            or np.any(capacities <= 0)
        ):
            raise ValueError("capacities must contain one positive finite value per link")
        if not np.allclose(capacities, capacities[0]):
            raise ValueError("Existing TE GB/AW accept a scalar link_cap; capacities must be uniform")
        matrices = np.asarray(inputs.demand_matrices, dtype=float)
        if matrices.ndim != 3 or matrices.shape[1:] != (len(inputs.graph), len(inputs.graph)):
            raise ValueError("demand_matrices must have shape (epochs, nodes, nodes)")
        if matrices.shape[0] == 0 or np.any(~np.isfinite(matrices)) or np.any(matrices < 0):
            raise ValueError("demand_matrices must be non-empty, finite, and non-negative")
        if len(inputs.path_output) < 5 or not inputs.path_output[0]:
            raise ValueError("path_output must be the existing five-part TE path result")
        if window < 1 or not 0 <= q <= 1 or not 0 <= hmax <= 1 or not 0 <= fixed_headroom < 1:
            raise ValueError("invalid headroom quantile, window, or reservation fraction")
        if not 0 < elephant_fraction <= 1 or not 0 < elephant_capacity_fraction <= 1:
            raise ValueError("elephant fractions must be in (0, 1]")
        self.capacity = float(capacities[0])
        self.edge_pairs = list(inputs.graph.edges())
        self.edge_idx = {edge: idx for idx, edge in enumerate(self.edge_pairs)}
        self.paths = inputs.path_output[0]
        self.path_output = inputs.path_output
        self.q, self.window, self.hmax = q, window, hmax
        self.fixed_headroom = fixed_headroom
        self.elephant_fraction = elephant_fraction
        self.elephant_capacity_fraction = elephant_capacity_fraction
        self.allocator = Allocator(inputs.graph, inputs.path_output, self.capacity)
        self.controllers = {
            name: Controller(self, name) for name in METHOD_NAMES
        }

    def link_load(self, allocation):
        loads = np.zeros(len(self.edge_pairs), dtype=float)
        for (src, dst), rates in allocation.items():
            for path, rate in zip(self.paths.get((src, dst), ()), rates):
                for u, v in zip(path, path[1:]):
                    if (u, v) in self.edge_idx:
                        loads[self.edge_idx[(u, v)]] += rate
        return loads

    def demand_link_load(self, demand):
        allocation = {}
        for _, (src, dst, amount) in self.allocator._problem(demand).sparse_commodity_list:
            routes = self.paths.get((src, dst), ())
            if routes:
                allocation[(src, dst)] = np.full(len(routes), amount / len(routes))
        return self.link_load(allocation)

    def _assert_capacity(self, allocation, capacity=None):
        loads = self.link_load(allocation)
        limit = self.capacity if capacity is None else float(capacity)
        assert np.all(loads <= limit + 1e-6), (
            f"Link capacity exceeded: max load {loads.max(initial=0):.6g}, "
            f"capacity {limit:.6g}"
        )
        return loads

    def _capacity_safe(self, allocation, demand, capacity):
        """Project approximate solver rates onto demand and link-capacity limits."""
        result = _copy_allocation(allocation)
        for pair, rates in result.items():
            flow_demand = float(demand[pair[0], pair[1]])
            total = float(np.sum(rates))
            if total > flow_demand and total > 0.0:
                result[pair] = rates * (flow_demand / total)
            result[pair] = np.maximum(result[pair], 0.0)

        loads = self.link_load(result)
        if np.any(loads > capacity):
            edge_factors = np.ones_like(loads)
            overloaded = loads > capacity
            edge_factors[overloaded] = capacity / loads[overloaded]
            for pair, rates in result.items():
                paths = self.paths.get(pair, ())
                for index, path in enumerate(paths[: rates.size]):
                    path_edges = [
                        self.edge_idx[edge]
                        for edge in zip(path, path[1:])
                        if edge in self.edge_idx
                    ]
                    if path_edges:
                        rates[index] *= float(np.min(edge_factors[path_edges]))
        self._assert_capacity(result, capacity)
        return result

    def _fairness(self, reference, allocation):
        if not reference:
            return 1.0
        score = 0.0
        compared = 0
        for pair in reference:
            baseline = float(np.sum(reference.get(pair, 0.0)))
            served = float(np.sum(allocation.get(pair, 0.0)))
            if baseline <= 0:
                continue
            a = max(FAIRNESS_FLOOR, baseline)
            b = max(FAIRNESS_FLOOR, served)
            score += np.log10(min(a / b, b / a))
            compared += 1
        return float(10 ** (score / max(1, compared)))

    @staticmethod
    def _served(allocation):
        return float(sum(np.sum(rates) for rates in allocation.values()))

    @staticmethod
    def _unmet(demand, allocation):
        served = {key: float(np.sum(value)) for key, value in allocation.items()}
        total = 0.0
        n = demand.shape[0]
        for src in range(n):
            for dst in range(n):
                if src != dst:
                    total += max(0.0, float(demand[src, dst]) - served.get((src, dst), 0.0))
        return total

    @staticmethod
    def _flow_metrics(demand, allocation):
        records = []
        fractions = []
        for src in range(demand.shape[0]):
            for dst in range(demand.shape[1]):
                if src == dst:
                    continue
                offered = float(demand[src, dst])
                served = float(np.sum(allocation.get((src, dst), ())))
                fraction = served / offered if offered > 1e-9 else None
                records.append({
                    "src": src,
                    "dst": dst,
                    "offered_demand": offered,
                    "served_rate": served,
                    "served_fraction": fraction,
                })
                if fraction is not None:
                    fractions.append(fraction)
        values = np.asarray(fractions, dtype=float)
        if values.size:
            squared_sum = float(np.sum(values * values))
            jain = (
                float(np.sum(values) ** 2 / (values.size * squared_sum))
                if squared_sum
                else float("nan")
            )
            minimum = float(np.min(values))
            p05 = float(np.quantile(values, 0.05))
        else:
            jain = minimum = p05 = float("nan")
        return {
            "per_flow_metrics_json": json.dumps(records, separators=(",", ":")),
            "jain_index": jain,
            "min_served_fraction": minimum,
            "p05_served_fraction": p05,
        }

    def _aw_delta(self, current, actual, forecast):
        result = _copy_allocation(current)
        delta = np.maximum(actual - forecast, 0.0)
        for (src, dst), rates in list(result.items()):
            limit = float(actual[src, dst])
            allocated = float(np.sum(rates))
            if allocated > limit and allocated > 0:
                result[(src, dst)] = rates * (limit / allocated)
        residual = self.capacity - self.link_load(result)
        aw_capacity = float(np.min(residual)) if residual.size else 0.0
        if np.any(delta > 1e-9) and aw_capacity > 1e-9:
            addition = self._capacity_safe(
                self.allocator.aw(delta, aw_capacity), delta, aw_capacity
            )
            for pair, rates in addition.items():
                if pair not in result:
                    result[pair] = np.zeros_like(rates)
                room = max(0.0, float(actual[pair]) - float(np.sum(result[pair])))
                amount = float(np.sum(rates))
                if amount > room and amount > 0:
                    rates = rates * (room / amount)
                result[pair] = result[pair] + rates
        return self._capacity_safe(result, actual, self.capacity)

    def run_scenario(
        self, demand_matrices, forecasts, *, controller_workers=DEFAULT_CONTROLLER_WORKERS,
        on_epoch_complete=None, on_checkpoint=None, start_epoch=0, initial_state=None,
        methods=METHOD_NAMES,
    ):
        if controller_workers < 1:
            raise ValueError("controller_workers must be positive")
        if not 0 <= start_epoch <= len(demand_matrices):
            raise ValueError("start_epoch must be within the demand sequence")
        methods = tuple(methods)
        if not methods or any(method not in METHOD_NAMES for method in methods):
            raise ValueError("methods must contain known controller names")
        if "periodic_gb" not in methods:
            raise ValueError("periodic_gb is required as the fairness reference")
        results = []
        previous = {name: {} for name in methods}
        degrees = np.asarray(
            [self.inputs.graph.degree(node) for node in range(len(self.inputs.graph))],
            dtype=float,
        )
        classifier = OnlineElephantMiceClassifier(
            demand_matrices[0], degrees, elephant_fraction=self.elephant_fraction
        )
        errors = deque(maxlen=self.window)
        headroom = np.zeros(len(self.edge_pairs), dtype=float)
        self._adaptive_high_epochs = 0
        self._adaptive_active = False
        self._cooldown_until = -1
        self._adaptive_gb_reruns = 0
        if initial_state is not None:
            previous = initial_state["previous"]
            classifier.load_state_dict(initial_state["classifier"])
            errors.extend(
                np.asarray(error, dtype=float).copy()
                for error in initial_state["errors"]
            )
            headroom = np.asarray(initial_state["headroom"], dtype=float).copy()
            self._adaptive_high_epochs = int(initial_state["adaptive_high_epochs"])
            self._adaptive_active = bool(initial_state["adaptive_active"])
            self._cooldown_until = int(initial_state["cooldown_until"])
            self._adaptive_gb_reruns = int(initial_state["adaptive_gb_reruns"])
        for epoch in range(start_epoch, len(demand_matrices)):
            actual = demand_matrices[epoch]
            reruns_before_epoch = self._adaptive_gb_reruns
            actual = np.asarray(actual, dtype=float)
            forecast = np.asarray(forecasts[epoch], dtype=float)
            forecast_link_load = self.demand_link_load(forecast)
            actual_link_load = self.demand_link_load(actual)
            error = actual_link_load - forecast_link_load
            errors.append(np.maximum(error, 0.0))
            if len(errors):
                headroom = np.clip(np.quantile(np.asarray(errors), self.q, axis=0), 0, self.hmax * self.capacity)
            self._headroom = headroom
            self._epoch = epoch
            ml_prediction = classifier.predict()
            current = {}
            runtimes = {}

            def run_controller(method):
                controller = self.controllers[method]
                controller.set_forecast(forecast)
                started = time.perf_counter()
                allocation = controller.run(
                    actual,
                    ml_prediction if method == "hybrid_ml_elephant_mice" else None,
                )
                return method, allocation, time.perf_counter() - started

            with ThreadPoolExecutor(
                max_workers=controller_workers,
                thread_name_prefix="te-controller",
            ) as executor:
                futures = [
                    executor.submit(run_controller, method)
                    for method in methods
                ]
                for future in as_completed(futures):
                    method, allocation, runtime = future.result()
                    current[method] = allocation
                    runtimes[method] = runtime
            reference = current["periodic_gb"]

            epoch_results = []
            for method in methods:
                allocation = current[method]
                loads = self._assert_capacity(allocation)
                unmet = self._unmet(actual, allocation)
                before = previous[method]
                changes = _churn(before, allocation)
                reruns = (
                    self._adaptive_gb_reruns - reruns_before_epoch
                    if method == "hybrid_adaptive"
                    else 0
                )
                row = {
                    "epoch": epoch,
                    "method": method,
                    "served_demand": self._served(allocation),
                    "unmet_demand": unmet,
                    **self._flow_metrics(actual, allocation),
                    "fairness_vs_periodic_gb": self._fairness(reference, allocation),
                    "capacity_violations": int(np.count_nonzero(loads > self.capacity + 1e-6)),
                    "runtime_s": runtimes[method],
                    "allocation_changes": changes,
                    "gb_reruns": reruns,
                }
                results.append(row)
                epoch_results.append(row)
                previous[method] = _copy_allocation(allocation)
            classifier.update(actual)
            if on_epoch_complete is not None:
                on_epoch_complete(epoch, epoch_results)
            if on_checkpoint is not None:
                on_checkpoint(
                    epoch,
                    epoch_results,
                    self.export_state(classifier, errors, previous),
                )
        return results

    def export_state(self, classifier, errors, previous):
        """Capture mutable per-scenario state at a completed epoch boundary."""
        return {
            "previous": previous,
            "classifier": classifier.state_dict(),
            "errors": [error.copy() for error in errors],
            "headroom": self._headroom.copy(),
            "adaptive_high_epochs": self._adaptive_high_epochs,
            "adaptive_active": self._adaptive_active,
            "cooldown_until": self._cooldown_until,
            "adaptive_gb_reruns": self._adaptive_gb_reruns,
        }

    def allocate(self, method, actual, forecast, ml_prediction=None):
        actual = np.asarray(actual, dtype=float)
        if method == "gb_only":
            return self._capacity_safe(
                self.allocator.gb(forecast, self.capacity), actual, self.capacity
            )
        if method == "aw_only":
            return self._capacity_safe(
                self.allocator.aw(actual, self.capacity), actual, self.capacity
            )
        if method == "periodic_gb":
            return self._capacity_safe(
                self.allocator.gb(actual, self.capacity), actual, self.capacity
            )
        if method == "hybrid_basic":
            baseline = self._capacity_safe(
                self.allocator.gb(forecast, self.capacity), forecast, self.capacity
            )
            return self._aw_delta(baseline, actual, forecast)
        if method == "hybrid_fixed":
            reserved = self.capacity * (1.0 - self.fixed_headroom)
            baseline = self._capacity_safe(
                self.allocator.gb(forecast, reserved), forecast, reserved
            )
            return self._aw_delta(baseline, actual, forecast)
        if method == "hybrid_adaptive":
            reserved = max(
                1e-6, self.capacity - float(np.max(self._headroom, initial=0.0))
            )
            baseline = self._capacity_safe(
                self.allocator.gb(forecast, reserved), forecast, reserved
            )
            result = self._aw_delta(baseline, actual, forecast)
            unmet_ratio = self._unmet(actual, result) / max(float(np.sum(actual)), 1e-9)
            if not self._adaptive_active and unmet_ratio >= 0.05:
                self._adaptive_active = True
                self._adaptive_high_epochs = 1
            elif self._adaptive_active and unmet_ratio >= 0.05:
                self._adaptive_high_epochs += 1
            if self._adaptive_active and unmet_ratio <= 0.02:
                self._adaptive_active = False
                self._adaptive_high_epochs = 0
            elif self._adaptive_active and unmet_ratio < 0.05:
                self._adaptive_high_epochs = 0
            if self._adaptive_high_epochs >= 2 and self._epoch >= self._cooldown_until:
                result = self._capacity_safe(
                    self.allocator.gb(actual, reserved), actual, reserved
                )
                self._adaptive_gb_reruns += 1
                self._adaptive_high_epochs = 0
                self._cooldown_until = self._epoch + 3
            return result
        if method == "hybrid_ml_elephant_mice":
            if ml_prediction is None:
                raise ValueError("ML demand partitions are required by the ML controller")
            predicted, elephant_demand, mice_demand = ml_prediction
            elephant_capacity = self.capacity * self.elephant_capacity_fraction
            elephant_allocation = self._capacity_safe(
                self.allocator.gb(elephant_demand, elephant_capacity),
                elephant_demand,
                elephant_capacity,
            )
            residual = self.capacity - self.link_load(elephant_allocation)
            mice_capacity = float(np.min(residual)) if residual.size else 0.0
            mice_allocation = self._capacity_safe(
                self.allocator.aw(mice_demand, mice_capacity),
                mice_demand,
                mice_capacity,
            )

            baseline = _copy_allocation(elephant_allocation)
            for pair, rates in mice_allocation.items():
                if pair not in baseline:
                    baseline[pair] = np.zeros_like(rates)
                baseline[pair] = baseline[pair] + rates
            self._assert_capacity(baseline)
            return self._aw_delta(
                baseline, actual, predicted
            )
        raise ValueError(f"Unknown controller: {method}")


def _churn(old, new, tolerance=1e-6):
    changes = 0
    for pair in set(old) | set(new):
        before = np.asarray(old.get(pair, ()), dtype=float)
        after = np.asarray(new.get(pair, ()), dtype=float)
        size = max(before.size, after.size)
        if size:
            before = np.pad(before, (0, size - before.size))
            after = np.pad(after, (0, size - after.size))
            changes += int(np.count_nonzero(np.abs(after - before) > tolerance))
    return changes


class MovingAverage:
    """Forecast from the most recent measured demand matrices."""

    def __init__(self, initial, window=3):
        self.initial = np.asarray(initial, dtype=float).copy()
        self.history = deque(maxlen=window)

    def predict(self):
        if not self.history:
            return self.initial.copy()
        return np.mean(np.asarray(self.history), axis=0)

    def observe(self, demand):
        self.history.append(np.asarray(demand, dtype=float).copy())


def generate_workload(seed, epochs, scenario, paths, graph, capacity):
    """Generate a periodic baseline with noise and one controlled shift."""
    rng = np.random.default_rng(seed)
    n = len(graph)
    baseline = rng.uniform(8.0, 16.0, size=(n, n))
    np.fill_diagonal(baseline, 0.0)
    factor = 1.0 + 0.12 * np.sin(2 * np.pi * np.arange(epochs) / 6.0)
    matrices = np.maximum(
        0.0,
        baseline[None, :, :] * factor[:, None, None]
        + rng.normal(0.0, 0.7, size=(epochs, n, n)),
    )
    np.fill_diagonal(matrices[0], 0.0)
    pivot = max(1, epochs // 2)
    a, b = 0, n - 1
    if scenario == "sudden_spike":
        matrices[pivot:, a, b] *= 4.0
    elif scenario == "hotspot_shift":
        matrices[:pivot, a, b] *= 3.0
        matrices[pivot:, a, b] *= 0.5
        matrices[pivot:, b, a] *= 3.0
    elif scenario == "demand_drop":
        matrices[pivot:] *= 0.35
    elif scenario == "near_saturation":
        probe = np.zeros_like(baseline)
        np.copyto(probe, baseline)
        loads = np.zeros(len(graph.edges), dtype=float)
        edge_idx = {edge: idx for idx, edge in enumerate(graph.edges())}
        for src in range(n):
            for dst in range(n):
                routes = paths.get((src, dst), ())
                if routes:
                    for route in routes:
                        for u, v in zip(route, route[1:]):
                            if (u, v) in edge_idx:
                                loads[edge_idx[(u, v)]] += probe[src, dst] / len(routes)
        scale = 0.85 * capacity / max(float(np.max(loads, initial=0.0)), 1e-9)
        matrices *= scale
    else:
        raise ValueError(f"Unknown scenario: {scenario}")
    for matrix in matrices:
        np.fill_diagonal(matrix, 0.0)
    return matrices


def _default_input(seed, epochs, scenario):
    import networkx as nx
    from ncflow.lib.problem import Problem
    from utilities import constants, shortest_paths

    graph = nx.DiGraph()
    graph.add_nodes_from(range(5))
    links = ((0, 1), (1, 0), (1, 2), (2, 1), (2, 3), (3, 2),
             (3, 4), (4, 3), (4, 0), (0, 4), (0, 2), (2, 0),
             (1, 3), (3, 1))
    graph.add_edges_from(links, capacity=100.0)
    capacity = 100.0
    seed_tm = np.ones((5, 5), dtype=float)
    np.fill_diagonal(seed_tm, 0.0)
    problem = Problem(graph, traffic_matrix=seed_tm, seed=seed)
    path_output = shortest_paths.all_pair_shortest_path_multi_processor(
        graph, problem.edge_idx, k=3, number_processors=1,
        base_split=0.9, split_type=constants.EXPONENTIAL_DECAY,
    )
    matrices = generate_workload(seed, epochs, scenario, path_output[0], graph, capacity)
    return ExperimentInput(
        graph=graph,
        path_output=path_output,
        capacities=np.full(len(graph.edges), capacity, dtype=float),
        demand_matrices=matrices,
    )


def choose_scenarios(topologies, traffic_models, scales, max_scenarios=0):
    """Resolve available PR2 matrix files for the requested matrix grid."""
    matrix_root = SOROUSH / "ncflow" / "traffic-matrices"
    topology_root = SOROUSH / "ncflow" / "topologies" / "topology-zoo"
    scenarios = []
    for topology in topologies:
        topology_file = topology_root / topology
        if not topology_file.is_file():
            raise FileNotFoundError(f"Topology does not exist: {topology_file}")
        for traffic_model in traffic_models:
            model_dir = matrix_root / traffic_model
            for scale in scales:
                matching = []
                for matrix_file in model_dir.glob(f"{topology}*_traffic-matrix.pkl"):
                    parts = matrix_file.stem.split("_")
                    if len(parts) > 3 and float(parts[3]) == float(scale):
                        matching.append(matrix_file)
                if not matching:
                    raise FileNotFoundError(
                        f"No {traffic_model} traffic matrix at scale {scale:g} "
                        f"for {topology}"
                    )
                scenarios.append(
                    (topology, traffic_model, float(scale), topology_file, sorted(matching)[0])
                )
    if max_scenarios:
        scenarios = scenarios[:max_scenarios]
    if not scenarios:
        raise ValueError("The selected topology/model/scale grid is empty")
    return scenarios


def _cached_paths(problem, topology, k, processors, cache_dir):
    from utilities import constants, shortest_paths

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{Path(topology).stem}_k{k}.pkl"
    signature = (tuple(problem.G.nodes()), tuple(problem.G.edges()))
    if cache_path.is_file():
        with cache_path.open("rb") as handle:
            cache = pickle.load(handle)
        if cache.get("signature") == signature:
            return cache["path_output"]

    path_output = shortest_paths.all_pair_shortest_path_multi_processor(
        problem.G,
        problem.edge_idx,
        k=k,
        number_processors=processors,
        base_split=0.9,
        split_type=constants.EXPONENTIAL_DECAY,
    )
    temporary = cache_path.with_suffix(".tmp")
    with temporary.open("wb") as handle:
        pickle.dump({"signature": signature, "path_output": path_output}, handle)
    temporary.replace(cache_path)
    return path_output


def _estimated_link_load(demand, paths, graph):
    edge_index = {edge: index for index, edge in enumerate(graph.edges())}
    loads = np.zeros(len(edge_index), dtype=float)
    for src, dst in np.argwhere(demand > 0):
        routes = paths.get((int(src), int(dst)), ())
        if not routes:
            continue
        rate = float(demand[src, dst]) / len(routes)
        for route in routes:
            for edge in zip(route, route[1:]):
                index = edge_index.get(edge)
                if index is not None:
                    loads[index] += rate
    return loads


def generate_dynamic_workload(
    seed, epochs, scenario, base_demand, paths, graph, *, scale, traffic_model
):
    """Create an epoch sequence with a controlled DCN traffic shift."""
    if scenario not in ALL_SCENARIOS:
        raise ValueError(f"Unknown scenario: {scenario}")
    rng = np.random.default_rng(seed)
    baseline = np.asarray(base_demand, dtype=float).copy()
    np.fill_diagonal(baseline, 0.0)
    if baseline.shape != (len(graph), len(graph)):
        raise ValueError("base_demand shape does not match the topology")

    if float(np.max(baseline, initial=0.0)) > 0 and float(scale) == 64.0:
        peak = float(np.max(_estimated_link_load(baseline, paths, graph), initial=0.0))
        if peak > 0:
            baseline *= 0.97 * LINK_CAPACITY / peak

    active = np.flatnonzero(baseline.reshape(-1) > 0.0)
    ranked = active[np.argsort(baseline.reshape(-1)[active])]
    heavy_indices = ranked[-max(1, int(np.ceil(0.15 * ranked.size))):]
    heavy_pairs = [np.unravel_index(index, baseline.shape) for index in heavy_indices]
    pivot = min(10, max(8, epochs // 2))
    matrices = np.empty((epochs, *baseline.shape), dtype=float)

    for epoch in range(epochs):
        noise_sigma = 0.22 if traffic_model == "poisson-high-inter" else 0.025
        noise = rng.lognormal(mean=-0.5 * noise_sigma ** 2, sigma=noise_sigma, size=baseline.shape)
        matrix = baseline * noise
        np.fill_diagonal(matrix, 0.0)
        if epoch >= pivot and scenario != NORMAL_SCENARIO:
            if scenario == "sudden_spike":
                for src, dst in heavy_pairs:
                    matrix[src, dst] *= 4.0
            elif scenario == "hotspot_shift":
                shifted = matrix.copy()
                for src, dst in heavy_pairs:
                    shifted[src, dst] *= 0.2
                    shifted[dst, src] += 3.0 * baseline[src, dst]
                matrix = shifted
            elif scenario == "dcn_incast":
                destination = int(np.argmax(np.sum(baseline, axis=0)))
                positive = baseline[baseline > 0]
                burst_floor = float(np.median(positive)) if positive.size else 0.0
                for src in range(len(graph)):
                    if src != destination:
                        matrix[src, destination] = max(
                            8.0 * matrix[src, destination], 4.0 * burst_floor
                        )
            elif scenario == "demand_drop":
                matrix *= 0.35
        np.fill_diagonal(matrix, 0.0)
        matrices[epoch] = matrix
    return matrices


def _write_checkpoint(path, checkpoint):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        pickle.dump(checkpoint, handle, protocol=pickle.HIGHEST_PROTOCOL)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _read_result_rows(path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(RESULT_COLUMNS):
            raise ValueError(f"{path} does not have the expected results.csv columns")
        return [
            row for row in reader
            if row.get("scenario_id") and row.get("epoch") is not None
        ]


def _write_result_rows(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def run_experiment(
    *,
    seed=0,
    epochs=DEFAULT_EPOCHS,
    topologies=TOPOLOGIES,
    traffic_models=TRAFFIC_MODELS,
    scales=LOAD_SCALES,
    scenarios=SCENARIOS,
    num_paths=16,
    path_processors=8,
    cache_dir=None,
    max_scenarios=0,
    controller_workers=DEFAULT_CONTROLLER_WORKERS,
    elephant_fraction=0.15,
    elephant_capacity_fraction=0.85,
    output_path=None,
    resume=False,
):
    from ncflow.lib.problem import Problem
    from utilities import utils

    selected = choose_scenarios(topologies, traffic_models, scales, max_scenarios)
    cache_dir = Path(cache_dir) if cache_dir else Path(__file__).resolve().parent / ".path-cache"
    paths_by_topology = {}
    rows = []
    total_scenarios = len(selected) * len(scenarios)
    total_epochs = total_scenarios * epochs
    resume_scenario_index = 0
    resume_epoch = 0
    resume_state = None
    output_file = None
    writer = None

    if output_path is not None:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path = output_path.with_suffix(output_path.suffix + ".checkpoint.pkl")
        run_config = {
            "seed": seed,
            "epochs": epochs,
            "selected": [
                (topology, model, scale, str(topology_file), str(matrix_file))
                for topology, model, scale, topology_file, matrix_file in selected
            ],
            "scenarios": list(scenarios),
            "num_paths": num_paths,
            "elephant_fraction": elephant_fraction,
            "elephant_capacity_fraction": elephant_capacity_fraction,
        }
        config_fingerprint = hashlib.sha256(
            json.dumps(run_config, sort_keys=True).encode("utf-8")
        ).hexdigest()

        if resume:
            if not checkpoint_path.is_file():
                raise FileNotFoundError(
                    f"Cannot resume: checkpoint not found at {checkpoint_path}"
                )
            if not output_path.is_file():
                raise FileNotFoundError(
                    f"Cannot resume: results CSV not found at {output_path}"
                )
            with checkpoint_path.open("rb") as handle:
                checkpoint = pickle.load(handle)
            if checkpoint.get("config_fingerprint") != config_fingerprint:
                raise ValueError(
                    "Resume settings do not match the saved run. Use the same "
                    "topologies, traffic models, scales, scenarios, epochs, seed, "
                    "and path count."
                )
            saved_scenario = int(checkpoint["scenario_index"])
            saved_epoch = int(checkpoint["epoch"])
            if not 0 <= saved_scenario < total_scenarios or not 0 <= saved_epoch < epochs:
                raise ValueError("Saved checkpoint position is outside this run")

            scenario_ids = [
                f"{topology}|{model}|{scale:g}|{scenario}"
                for topology, model, scale, _, _ in selected
                for scenario in scenarios
            ]
            scenario_positions = {
                scenario_id: index for index, scenario_id in enumerate(scenario_ids)
            }
            existing_rows = _read_result_rows(output_path)
            rows = [
                row for row in existing_rows
                if row["scenario_id"] in scenario_positions
                and (
                    scenario_positions[row["scenario_id"]] < saved_scenario
                    or (
                        scenario_positions[row["scenario_id"]] == saved_scenario
                        and int(row["epoch"]) < saved_epoch
                    )
                )
            ]
            rows.extend(checkpoint["rows"])
            _write_result_rows(output_path, rows)

            if saved_epoch + 1 == epochs:
                resume_scenario_index = saved_scenario + 1
            else:
                resume_scenario_index = saved_scenario
                resume_epoch = saved_epoch + 1
                resume_state = checkpoint["state"]
            completed_epochs = min(
                total_epochs,
                saved_scenario * epochs + saved_epoch + 1,
            )
            print(
                f"Resuming after scenario {saved_scenario + 1}/{total_scenarios}, "
                f"epoch {saved_epoch + 1}/{epochs}",
                flush=True,
            )
            output_file = output_path.open("a", encoding="utf-8", newline="")
        else:
            if checkpoint_path.exists():
                checkpoint_path.unlink()
            output_file = output_path.open("w", encoding="utf-8", newline="")
            writer = csv.DictWriter(output_file, fieldnames=RESULT_COLUMNS)
            writer.writeheader()
            output_file.flush()
            completed_epochs = 0
            print(f"Writing completed epochs incrementally to {output_path}", flush=True)

        if resume:
            writer = csv.DictWriter(output_file, fieldnames=RESULT_COLUMNS)
        progress = _ProgressReporter(
            total_scenarios, epochs, completed_epochs=completed_epochs
        )
    else:
        if resume:
            raise ValueError("--resume requires an output CSV path")
        progress = _ProgressReporter(total_scenarios, epochs)

    try:
        for case_index, (topology, model, scale, topology_file, matrix_file) in enumerate(selected):
            if (case_index + 1) * len(scenarios) <= resume_scenario_index:
                continue
            print(f"Preparing {topology} | {model} | scale={scale:g} from {matrix_file.name}", flush=True)
            problem = Problem.from_file(str(topology_file), str(matrix_file))
            utils.revise_list_commodities(problem)
            if topology not in paths_by_topology:
                print(f"Computing/loading k={num_paths} candidate paths for {topology}", flush=True)
                paths_by_topology[topology] = _cached_paths(
                    problem, topology, num_paths, path_processors, cache_dir
                )
            path_output = paths_by_topology[topology]
            for shift_index, scenario in enumerate(scenarios):
                scenario_index = case_index * len(scenarios) + shift_index
                if scenario_index < resume_scenario_index:
                    continue
                matrices = generate_dynamic_workload(
                    seed + case_index * len(scenarios) + shift_index,
                    epochs,
                    scenario,
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
                scenario_id = f"{topology}|{model}|{scale:g}|{scenario}"
                scenario_metadata = {
                    "scenario": scenario,
                    "scenario_id": scenario_id,
                    "topology": topology,
                    "traffic_model": model,
                    "scale_factor": float(scale),
                    "traffic_matrix": matrix_file.name,
                    "num_demands": int(np.count_nonzero(problem.traffic_matrix.tm)),
                    "num_paths": num_paths,
                }
                scenario_start_epoch = (
                    resume_epoch if scenario_index == resume_scenario_index else 0
                )
                scenario_initial_state = (
                    resume_state if scenario_index == resume_scenario_index else None
                )

                def save_epoch(epoch, epoch_rows, state):
                    for row in epoch_rows:
                        row.update(scenario_metadata)
                    if writer is not None:
                        _write_checkpoint(
                            checkpoint_path,
                            {
                                "config_fingerprint": config_fingerprint,
                                "scenario_index": scenario_index,
                                "epoch": epoch,
                                "state": state,
                                "rows": epoch_rows,
                            },
                        )
                        writer.writerows(epoch_rows)
                        output_file.flush()
                        os.fsync(output_file.fileno())
                    rows.extend(epoch_rows)
                    progress.update(scenario_index, scenario_id, epoch)

                Experiment(
                    inputs,
                    q=0.9,
                    window=8,
                    elephant_fraction=elephant_fraction,
                    elephant_capacity_fraction=elephant_capacity_fraction,
                ).run_scenario(
                    matrices,
                    forecasts,
                    controller_workers=controller_workers,
                    on_checkpoint=save_epoch,
                    start_epoch=scenario_start_epoch,
                    initial_state=scenario_initial_state,
                )
    finally:
        if output_file is not None:
            output_file.close()
    if output_path is not None:
        return pd.read_csv(output_path)
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--epochs", type=int, default=DEFAULT_EPOCHS)
    parser.add_argument("--scenario", choices=("all",) + SCENARIOS, default="all")
    parser.add_argument("--topologies", default=",".join(TOPOLOGIES))
    parser.add_argument("--traffic-models", default=",".join(TRAFFIC_MODELS))
    parser.add_argument("--scales", default="8,16,32,64")
    parser.add_argument("--num-paths", type=int, default=16)
    parser.add_argument("--path-processors", type=int, default=8)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--max-scenarios", type=int, default=0)
    parser.add_argument("--controller-workers", type=int, default=DEFAULT_CONTROLLER_WORKERS)
    parser.add_argument("--elephant-fraction", type=float, default=0.15)
    parser.add_argument("--elephant-capacity-fraction", type=float, default=0.85)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "results.csv",
        help="Per-epoch CSV output path; use --resume to continue its checkpoint",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from the checkpoint beside the selected output CSV",
    )
    parser.add_argument("--plot", action="store_true")
    args = parser.parse_args()
    if not 15 <= args.epochs <= 20:
        parser.error("--epochs must be between 15 and 20")
    if args.num_paths < 1 or args.path_processors < 1 or args.controller_workers < 1:
        parser.error("--num-paths, --path-processors, and --controller-workers must be positive")
    if not 0 < args.elephant_fraction <= 1 or not 0 < args.elephant_capacity_fraction <= 1:
        parser.error("elephant fractions must be in (0, 1]")
    scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
    results = run_experiment(
        seed=args.seed,
        epochs=args.epochs,
        topologies=tuple(item.strip() for item in args.topologies.split(",")),
        traffic_models=tuple(item.strip() for item in args.traffic_models.split(",")),
        scales=tuple(float(item.strip()) for item in args.scales.split(",")),
        scenarios=scenarios,
        num_paths=args.num_paths,
        path_processors=args.path_processors,
        cache_dir=args.cache_dir,
        max_scenarios=args.max_scenarios,
        controller_workers=args.controller_workers,
        elephant_fraction=args.elephant_fraction,
        elephant_capacity_fraction=args.elephant_capacity_fraction,
        output_path=args.output,
        resume=args.resume,
    )
    output = args.output.resolve().parent
    summary = results.groupby(["topology", "traffic_model", "scale_factor", "scenario", "method"], as_index=False).agg(
        served_demand=("served_demand", "mean"),
        unmet_demand=("unmet_demand", "mean"),
        jain_index=("jain_index", "mean"),
        min_served_fraction=("min_served_fraction", "mean"),
        p05_served_fraction=("p05_served_fraction", "mean"),
        fairness_vs_periodic_gb=("fairness_vs_periodic_gb", "mean"),
        capacity_violations=("capacity_violations", "sum"),
        runtime_s=("runtime_s", "mean"),
        allocation_changes=("allocation_changes", "sum"),
        gb_reruns=("gb_reruns", "sum"),
    )
    print(summary.to_string(index=False, float_format=lambda value: f"{value:.3f}"))
    if args.plot:
        import matplotlib.pyplot as plt

        summary.pivot(index="method", columns="scenario", values="served_demand").plot(
            kind="bar", figsize=(10, 5)
        )
        plt.tight_layout()
        plt.savefig(output / "results.png", dpi=150)
        plt.close()


if __name__ == "__main__":
    main()
