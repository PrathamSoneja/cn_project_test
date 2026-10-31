"""Online demand predictor and elephant/mice partitioner."""
from __future__ import annotations

import numpy as np


class OnlineElephantMiceClassifier:
    """Predict the next-epoch top-volume flows from online demand features."""

    def __init__(
        self,
        initial_demand: np.ndarray,
        degrees: np.ndarray,
        *,
        beta: float = 0.7,
        window: int = 5,
        elephant_fraction: float = 0.15,
    ) -> None:
        initial = np.asarray(initial_demand, dtype=float)
        degree = np.asarray(degrees, dtype=float)
        if initial.ndim != 2 or initial.shape[0] != initial.shape[1]:
            raise ValueError("initial_demand must be a square matrix")
        if degree.shape != (initial.shape[0],):
            raise ValueError("degrees must have one value per node")
        if not 0.0 < beta <= 1.0 or window < 1 or not 0.0 < elephant_fraction <= 1.0:
            raise ValueError("invalid EWMA, rolling-window, or elephant-fraction setting")
        if np.any(~np.isfinite(initial)) or np.any(initial < 0):
            raise ValueError("initial_demand must be finite and non-negative")

        self.beta = beta
        self.window = window
        self.elephant_fraction = elephant_fraction
        self._previous = initial.copy()
        self._ewma = initial.copy()
        self._history = [initial.copy()]
        degree_scale = float(np.max(degree, initial=0.0))
        self._centrality = degree / degree_scale if degree_scale > 0 else degree
        self._pair_centrality = (
            self._centrality[:, None] + self._centrality[None, :]
        ) * 0.5
        np.fill_diagonal(self._pair_centrality, 0.0)

    def predict(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Return predicted demand and its elephant/mice demand partitions."""
        momentum = self._previous - self._history[-2] if len(self._history) > 1 else 0.0
        variance = (
            np.var(np.asarray(self._history[-self.window :]), axis=0)
            if len(self._history) > 1
            else np.zeros_like(self._ewma)
        )
        predicted = np.maximum(
            0.0,
            self._ewma
            + momentum
            + 0.1 * np.sqrt(variance),
        )
        predicted *= 1.0 + 0.01 * self._pair_centrality
        np.fill_diagonal(predicted, 0.0)

        flat = predicted.reshape(-1)
        active = np.flatnonzero(flat > 0.0)
        elephant_count = int(np.ceil(self.elephant_fraction * active.size))
        elephant_indices = np.zeros(flat.size, dtype=bool)
        if elephant_count:
            largest = np.argpartition(flat[active], -elephant_count)[-elephant_count:]
            elephant_indices[active[largest]] = True

        elephant = np.zeros_like(predicted)
        mice = predicted.copy()
        elephant.reshape(-1)[elephant_indices] = flat[elephant_indices]
        mice.reshape(-1)[elephant_indices] = 0.0
        return predicted, elephant, mice

    def update(self, demand: np.ndarray) -> None:
        """Incorporate the just-observed epoch before forecasting the next."""
        observed = np.asarray(demand, dtype=float).copy()
        if observed.shape != self._ewma.shape:
            raise ValueError("demand shape changed during online prediction")
        if np.any(~np.isfinite(observed)) or np.any(observed < 0):
            raise ValueError("demand must be finite and non-negative")
        np.fill_diagonal(observed, 0.0)
        self._ewma = self.beta * observed + (1.0 - self.beta) * self._ewma
        self._history.append(observed.copy())
        if len(self._history) > self.window:
            self._history.pop(0)
        self._previous = observed.copy()

    def state_dict(self) -> dict:
        """Return the mutable predictor state needed to resume a scenario."""
        return {
            "previous": self._previous.copy(),
            "ewma": self._ewma.copy(),
            "history": [matrix.copy() for matrix in self._history],
        }

    def load_state_dict(self, state: dict) -> None:
        """Restore predictor state, rejecting incompatible saved matrices."""
        previous = np.asarray(state["previous"], dtype=float)
        ewma = np.asarray(state["ewma"], dtype=float)
        history = [np.asarray(matrix, dtype=float) for matrix in state["history"]]
        if (
            previous.shape != self._previous.shape
            or ewma.shape != self._ewma.shape
            or not history
            or any(matrix.shape != self._ewma.shape for matrix in history)
            or len(history) > self.window
            or np.any(~np.isfinite(previous))
            or np.any(~np.isfinite(ewma))
            or any(np.any(~np.isfinite(matrix)) for matrix in history)
        ):
            raise ValueError("saved classifier state is incompatible with this scenario")
        self._previous = previous.copy()
        self._ewma = ewma.copy()
        self._history = [matrix.copy() for matrix in history]
