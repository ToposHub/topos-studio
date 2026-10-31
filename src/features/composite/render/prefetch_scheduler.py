"""
Prefetch Scheduler - Intelligent cache prefetch based on parameter trend analysis.

Analyzes parameter change patterns during UI interactions (slider dragging)
to predict likely future parameter values and prefetch downstream node results.

Key concepts:
- Trend analysis: Track recent parameter changes (last 5 values)
- Prediction: Extrapolate trend to predict next value
- Prefetch trigger: During debounce period, prefetch predicted value
- Category: Prefetched entries use CacheCategory.PREFETCH (short-lived)
- Promotion: On hit, PREFETCH entries are promoted to EFFECT (long-lived)

Workflow:
1. UI slider drag -> record parameter value history
2. Debounce timer starts -> analyze trend, predict next value
3. Prefetch timer fires (40ms after debounce) -> submit prefetch request
4. User settles on value -> if matches prediction, cache hit + promotion
5. If not matching -> PREFETCH entries are cleared (prediction failed)
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.graph_snapshot import GraphSnapshot


@dataclass
class ParamHistory:
    """History of recent parameter values for trend analysis.

    Attributes:
        values: Recent parameter values (up to max_history).
        timestamps: Timestamps for each value (monotonic time).
        node_id: Node ID this parameter belongs to.
        param_name: Parameter name.
        trend_direction: Current trend direction (+1, -1, 0).
        velocity: Rate of change per ms（时间戳为 time.monotonic() 秒制，
            记录时换算为 units/ms，AUD-8408）.
    """
    values: deque = field(default_factory=lambda: deque(maxlen=5))
    timestamps: deque = field(default_factory=lambda: deque(maxlen=5))
    node_id: str = ""
    param_name: str = ""
    trend_direction: int = 0  # +1 = increasing, -1 = decreasing, 0 = stable
    velocity: float = 0.0  # units per millisecond (AUD-8408: 换算自秒制时间戳)


@dataclass
class PrefetchPrediction:
    """A predicted parameter value for prefetch.

    Attributes:
        node_id: Node ID.
        param_name: Parameter name.
        predicted_value: Predicted next value.
        confidence: Confidence level (0.0-1.0).
        based_on_trend: True if based on trend analysis, False if fallback.
    """
    node_id: str
    param_name: str
    predicted_value: float
    confidence: float
    based_on_trend: bool


@dataclass
class PrefetchStats:
    """Statistics for prefetch performance monitoring."""
    predictions_made: int = 0
    predictions_hit: int = 0
    predictions_miss: int = 0
    prefetch_requests_sent: int = 0
    prefetch_time_saved_ms: float = 0.0

    @property
    def hit_rate(self) -> float:
        total = self.predictions_hit + self.predictions_miss
        return self.predictions_hit / total if total > 0 else 0.0


class PrefetchScheduler:
    """Manages parameter trend analysis and cache prefetch scheduling.

    Thread safety: Lives on the UI thread, interacts with render thread
    via PrefetchRequest messages.

    Usage:
        scheduler = PrefetchScheduler()
        scheduler.record_param_change(node_id, param_name, value, timestamp)
        prediction = scheduler.predict_next_value(node_id, param_name)
        if prediction and prediction.confidence > 0.5:
            scheduler.trigger_prefetch(prediction, snapshot)
    """

    # Minimum confidence threshold to trigger prefetch
    MIN_CONFIDENCE_THRESHOLD = 0.4

    # Maximum velocity threshold (if too fast, prediction unreliable)
    MAX_VELOCITY_THRESHOLD = 0.01  # units per ms

    # History size for trend analysis
    MAX_HISTORY_SIZE = 5

    def __init__(self) -> None:
        self._param_histories: Dict[Tuple[str, str], ParamHistory] = {}
        self._active_prediction: Optional[PrefetchPrediction] = None
        self._stats = PrefetchStats()
        self._prefetch_callback: Optional[Callable] = None

    def set_prefetch_callback(self, callback: Callable):
        """Set the callback function to trigger prefetch requests.

        Args:
            callback: Function that takes (node_id, param_name, predicted_value)
                      and triggers a prefetch render request.
        """
        self._prefetch_callback = callback

    # ── Parameter Bounds Helpers ──

    @staticmethod
    def get_param_bounds(node: Any, param_name: str) -> Optional[Tuple[float, float]]:
        """Extract parameter bounds (min, max) from a node's ParamDef.

        This is a helper method for callers to obtain valid parameter bounds
        to pass to predict_next_value().

        Args:
            node: Node object with PARAM_DEFS or get_param_defs() method.
            param_name: Parameter name to get bounds for.

        Returns:
            Tuple of (min_value, max_value) if ParamDef exists, None otherwise.
            For non-numeric parameters (str, bool, option), returns None.

        Example:
            from ..nodes.base_node import ParamDef
            bounds = PrefetchScheduler.get_param_bounds(node, "radius")
            prediction = scheduler.predict_next_value(node_id, "radius", value, bounds)
        """
        # Get ParamDef from node
        param_defs = None
        if hasattr(node, 'get_param_defs'):
            param_defs = node.get_param_defs()
        elif hasattr(node, 'PARAM_DEFS'):
            # PARAM_DEFS is a list, convert to dict
            param_defs = {pdef.name: pdef for pdef in node.PARAM_DEFS}

        if param_defs is None:
            return None

        pdef = param_defs.get(param_name)
        if pdef is None:
            return None

        # Only numeric types have bounds
        if hasattr(pdef, 'ptype') and pdef.ptype not in ('float', 'int'):
            return None

        # Extract bounds from ParamDef
        min_val = getattr(pdef, 'min_val', 0.0)
        max_val = getattr(pdef, 'max_val', 1.0)

        # Ensure bounds are valid
        if min_val is None:
            min_val = 0.0
        if max_val is None:
            max_val = 1.0

        return (float(min_val), float(max_val))

    # ── Trend Analysis ──

    def record_param_change(self, node_id: str, param_name: str,
                            value: float, timestamp: float):
        """Record a parameter value change for trend analysis.

        Called from UI thread during slider drag interactions.

        Args:
            node_id: Node ID.
            param_name: Parameter name.
            value: Current parameter value.
            timestamp: Monotonic timestamp (time.monotonic()).
        """
        key = (node_id, param_name)

        # Get or create history
        history = self._param_histories.get(key)
        if history is None:
            history = ParamHistory(
                node_id=node_id,
                param_name=param_name,
                values=deque(maxlen=self.MAX_HISTORY_SIZE),
                timestamps=deque(maxlen=self.MAX_HISTORY_SIZE),
            )
            self._param_histories[key] = history

        # Append to history
        history.values.append(value)
        history.timestamps.append(timestamp)

        # Update trend analysis
        self._analyze_trend(history)

    def _analyze_trend(self, history: ParamHistory):
        """Analyze trend direction and velocity from recent values.

        Uses linear regression on recent values to determine:
        - Direction: increasing (+1), decreasing (-1), or stable (0)
        - Velocity: rate of change per millisecond
        """
        if len(history.values) < 2:
            history.trend_direction = 0
            history.velocity = 0.0
            return

        # Calculate direction from last two values
        last_val = history.values[-1]
        prev_val = history.values[-2]
        diff = last_val - prev_val

        if abs(diff) < 0.001:  # Threshold for "stable"
            history.trend_direction = 0
            history.velocity = 0.0
            return

        history.trend_direction = 1 if diff > 0 else -1

        # Calculate velocity (units per ms)
        # AUD-8408: timestamps come from time.monotonic() (seconds). The
        # old code divided by the raw diff, producing units/SECOND while
        # MAX_VELOCITY_THRESHOLD (0.01 units/ms = 10 units/s) assumed ms —
        # the threshold was effectively 1000x tighter than intended and
        # the trend path was unreachable at normal drag speeds. Convert
        # to units/ms so the unit matches the threshold's calibration.
        if len(history.timestamps) >= 2:
            time_diff = history.timestamps[-1] - history.timestamps[-2]
            if time_diff > 0:
                history.velocity = abs(diff) / (time_diff * 1000.0)
            else:
                history.velocity = 0.0

    def predict_next_value(self, node_id: str, param_name: str,
                           current_value: float,
                           param_bounds: Optional[Tuple[float, float]] = None
                           ) -> Optional[PrefetchPrediction]:
        """Predict the next parameter value based on trend analysis.

        Args:
            node_id: Node ID.
            param_name: Parameter name.
            current_value: Current parameter value.
            param_bounds: Optional tuple of (min_value, max_value) for clamping.
                          If None, defaults to (0.0, 1.0) for normalized parameters.
                          Can be obtained from the node's ParamDef via
                          get_param_bounds() helper method.

        Returns:
            PrefetchPrediction with predicted value and confidence,
            or None if no reliable prediction can be made.
        """
        key = (node_id, param_name)
        history = self._param_histories.get(key)

        if history is None or len(history.values) < 2:
            # No history, try fallback prediction (step-based)
            return self._fallback_prediction(node_id, param_name, current_value)

        # Check velocity threshold - if too fast, prediction unreliable
        if history.velocity > self.MAX_VELOCITY_THRESHOLD:
            return self._fallback_prediction(node_id, param_name, current_value)

        # Linear extrapolation from trend
        if len(history.values) >= 3:
            # Use linear regression on last 3+ values
            predicted, confidence = self._linear_extrapolation(history)
        else:
            # Simple extrapolation from last two values
            predicted, confidence = self._simple_extrapolation(history)

        # Confidence adjustment based on history size
        history_factor = len(history.values) / self.MAX_HISTORY_SIZE
        confidence *= history_factor

        # Clamp prediction to parameter bounds
        # Use provided bounds or fall back to reasonable defaults
        min_bound, max_bound = param_bounds if param_bounds else (0.0, 1.0)
        predicted = max(min_bound, min(max_bound, predicted))

        self._stats.predictions_made += 1

        return PrefetchPrediction(
            node_id=node_id,
            param_name=param_name,
            predicted_value=predicted,
            confidence=confidence,
            based_on_trend=True,
        )

    def _simple_extrapolation(self, history: ParamHistory) -> Tuple[float, float]:
        """Simple extrapolation using last two values.

        Returns:
            Tuple of (predicted_value, confidence).
        """
        last_val = history.values[-1]
        prev_val = history.values[-2]
        diff = last_val - prev_val

        # Predict: next = current + diff (one step extrapolation)
        predicted = last_val + diff

        # Confidence: higher if velocity is stable
        # (compare last two diffs if available)
        if len(history.values) >= 3:
            prev_prev_val = history.values[-3]
            prev_diff = prev_val - prev_prev_val
            # If diffs are similar, higher confidence
            diff_similarity = 1.0 - abs(abs(diff) - abs(prev_diff)) / (abs(diff) + abs(prev_diff) + 0.001)
            confidence = 0.5 * diff_similarity + 0.3
        else:
            confidence = 0.4  # Base confidence for simple extrapolation

        return predicted, confidence

    def _linear_extrapolation(self, history: ParamHistory) -> Tuple[float, float]:
        """Linear regression extrapolation using last 3+ values.

        Returns:
            Tuple of (predicted_value, confidence).
        """
        n = len(history.values)
        if n < 3:
            return self._simple_extrapolation(history)

        # Simple linear regression: y = a + b*x
        # x = time (normalized), y = value
        values = list(history.values)

        # Normalize time to 0..n-1
        x_vals = list(range(n))
        y_vals = values

        # Calculate slope (b) and intercept (a)
        x_mean = sum(x_vals) / n
        y_mean = sum(y_vals) / n

        numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, y_vals))
        denominator = sum((x - x_mean) ** 2 for x in x_vals)

        if denominator == 0:
            return y_vals[-1], 0.3

        slope = numerator / denominator
        intercept = y_mean - slope * x_mean

        # Predict next value at x = n
        predicted = intercept + slope * n

        # Confidence based on R^2 correlation coefficient
        y_pred = [intercept + slope * x for x in x_vals]
        ss_res = sum((y - y_pred[i]) ** 2 for i, y in enumerate(y_vals))
        ss_tot = sum((y - y_mean) ** 2 for y in y_vals)

        if ss_tot == 0:
            r_squared = 1.0
        else:
            r_squared = 1.0 - (ss_res / ss_tot)

        # R^2 ranges from 0 to 1, use as confidence base
        confidence = max(0.3, min(0.9, r_squared))

        return predicted, confidence

    def _fallback_prediction(self, node_id: str, param_name: str,
                             current_value: float) -> Optional[PrefetchPrediction]:
        """Fallback prediction when trend analysis is insufficient.

        Uses a step-based approach: predict small steps in the direction
        of recent movement if available, otherwise no prediction.

        Returns:
            PrefetchPrediction with low confidence, or None.
        """
        key = (node_id, param_name)
        history = self._param_histories.get(key)

        if history and len(history.values) >= 1:
            last_val = history.values[-1]

            # If we have at least one previous value, predict a small step
            if len(history.values) >= 2:
                prev_val = history.values[-2]
                diff = last_val - prev_val
                if abs(diff) > 0.001:
                    # Predict continuing in the same direction with smaller step
                    predicted = last_val + diff * 0.5  # Half step
                    return PrefetchPrediction(
                        node_id=node_id,
                        param_name=param_name,
                        predicted_value=predicted,
                        confidence=0.2,  # Low confidence
                        based_on_trend=False,
                    )

        # No prediction available
        return None

    # ── Prefetch Trigger ──

    def should_prefetch(self, prediction: Optional[PrefetchPrediction]) -> bool:
        """Check if a prediction should trigger a prefetch request.

        Args:
            prediction: The prediction to evaluate.

        Returns:
            True if prefetch should be triggered.
        """
        if prediction is None:
            return False

        return prediction.confidence >= self.MIN_CONFIDENCE_THRESHOLD

    def trigger_prefetch(self, prediction: PrefetchPrediction,
                         snapshot: Optional["GraphSnapshot"] = None):
        """Trigger a prefetch request for the predicted value.

        Args:
            prediction: The prediction to prefetch.
            snapshot: Optional graph snapshot for the prefetch request.
        """
        if self._prefetch_callback is None:
            return

        # Store active prediction for hit/miss tracking
        self._active_prediction = prediction
        self._stats.prefetch_requests_sent += 1

        # Call the prefetch callback
        self._prefetch_callback(
            prediction.node_id,
            prediction.param_name,
            prediction.predicted_value,
            snapshot,
        )

    def check_prediction_hit(self, node_id: str, param_name: str,
                             actual_value: float) -> bool:
        """Check if the active prediction matched the actual value.

        Called when user settles on a final value after debounce.

        Args:
            node_id: Node ID.
            param_name: Parameter name.
            actual_value: The actual value user settled on.

        Returns:
            True if prediction was accurate (hit), False otherwise.
        """
        if self._active_prediction is None:
            return False

        # Check if prediction matches
        if (self._active_prediction.node_id == node_id and
            self._active_prediction.param_name == param_name):

            # Allow small tolerance for hit detection
            tolerance = 0.01  # 1% tolerance
            diff = abs(self._active_prediction.predicted_value - actual_value)
            max_allowed = max(abs(actual_value) * tolerance, 0.005)

            if diff <= max_allowed:
                self._stats.predictions_hit += 1
                self._active_prediction = None
                return True
            else:
                self._stats.predictions_miss += 1
                self._active_prediction = None
                return False

        return False

    # ── History Management ──

    def clear_history(self, node_id: Optional[str] = None,
                      param_name: Optional[str] = None):
        """Clear parameter history.

        Args:
            node_id: If specified, clear only for this node.
            param_name: If specified with node_id, clear only this param.
        """
        if node_id is None:
            self._param_histories.clear()
            return

        if param_name is None:
            # Clear all params for this node
            keys_to_remove = [k for k in self._param_histories if k[0] == node_id]
            for k in keys_to_remove:
                del self._param_histories[k]
        else:
            # Clear specific param
            key = (node_id, param_name)
            if key in self._param_histories:
                del self._param_histories[key]

    def reset(self):
        """Reset all state (histories, predictions, stats)."""
        self._param_histories.clear()
        self._active_prediction = None
        # Keep stats for monitoring

    # ── Stats ──

    @property
    def stats(self) -> PrefetchStats:
        return self._stats

    def dump_stats(self) -> str:
        """Formatted statistics for debugging."""
        s = self._stats
        return (
            f"[PrefetchScheduler] predictions={s.predictions_made} "
            f"hits={s.predictions_hit} misses={s.predictions_miss} "
            f"hit_rate={s.hit_rate:.0%} requests_sent={s.prefetch_requests_sent} "
            f"time_saved={s.prefetch_time_saved_ms:.1f}ms"
        )
