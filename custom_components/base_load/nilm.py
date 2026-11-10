"""Pure logic for appliance detection from a live house load signal (NILM light).

Pipeline:
1. SteadyStateDetector splits the signal into steady segments and reports an
   edge whenever a new steady level differs from the previous one.
2. EventPairer matches an "on" edge with a later "off" edge of similar size
   and turns the pair into a run: power, start, duration.
3. ApplianceCatalog groups runs of similar power and duration into clusters.
   A cluster the user names becomes an appliance with its own power and
   energy sensors.

Only clearly switched loads are found: steps of at least the minimum step
(default 80 W) that stay for a few seconds. Small, modulating or slowly
drifting loads stay in the unexplained rest.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import math
from statistics import fmean
from typing import Any
import uuid

# Steady-state detection.
STEADY_TOLERANCE_W = 30.0
STEADY_TOLERANCE_REL = 0.05
STEADY_MIN_SAMPLES = 2

# Pairing an off edge with an earlier on edge.
PAIR_TOLERANCE_W = 40.0
PAIR_TOLERANCE_REL = 0.15
MAX_OPEN_EDGES = 20
MAX_RUN = timedelta(hours=12)

# Clustering runs.
CLUSTER_POWER_TOLERANCE = 0.12  # relative, on a log scale
CLUSTER_DURATION_FACTOR = 3.0  # durations within a factor of 3
CLUSTER_SMOOTHING = 20  # running average over about this many runs
MAX_CLUSTERS = 60


@dataclass(frozen=True, slots=True)
class Edge:
    """A change between two steady levels."""

    time: datetime
    delta: float
    before: float
    after: float


@dataclass(frozen=True, slots=True)
class Run:
    """An appliance run: switched on, stayed on, switched off."""

    start: datetime
    end: datetime
    power: float

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def energy_wh(self) -> float:
        return self.power * self.duration.total_seconds() / 3600


class SteadyStateDetector:
    """Report edges between steady levels of a noisy signal."""

    def __init__(self, min_step: float) -> None:
        self.min_step = min_step
        self._segment: list[float] = []
        self._segment_start: datetime | None = None
        self._level: float | None = None
        self._edge_reported = False

    @property
    def level(self) -> float | None:
        """Last steady level."""
        return self._level

    @staticmethod
    def _tolerance(level: float) -> float:
        return max(STEADY_TOLERANCE_W, abs(level) * STEADY_TOLERANCE_REL)

    def feed(self, time: datetime, value: float) -> Edge | None:
        """Add a sample; returns an edge once a new steady level is confirmed."""
        if self._segment:
            mean = fmean(self._segment)
            if abs(value - mean) <= self._tolerance(mean):
                self._segment.append(value)
                return self._check_steady()
            self._close_segment()
        self._segment = [value]
        self._segment_start = time
        self._edge_reported = False
        return self._check_steady()

    def _check_steady(self) -> Edge | None:
        if len(self._segment) != STEADY_MIN_SAMPLES or self._segment_start is None:
            return None
        mean = fmean(self._segment)
        previous = self._level
        self._level = mean
        if previous is None or abs(mean - previous) < self.min_step:
            return None
        self._edge_reported = True
        return Edge(time=self._segment_start, delta=mean - previous, before=previous, after=mean)

    def _close_segment(self) -> None:
        # A segment that became steady refines the level with all its samples.
        if len(self._segment) >= STEADY_MIN_SAMPLES:
            self._level = fmean(self._segment)


class EventPairer:
    """Turn on and off edges into runs."""

    def __init__(self) -> None:
        self.open: list[tuple[datetime, float]] = []

    @staticmethod
    def matches(power: float, step: float) -> bool:
        return abs(power - step) <= max(PAIR_TOLERANCE_W, power * PAIR_TOLERANCE_REL)

    def feed(self, edge: Edge) -> Run | None:
        """An on edge opens a run, a matching off edge closes the closest one."""
        self.open = [(t, p) for t, p in self.open if edge.time - t <= MAX_RUN]
        if edge.delta > 0:
            self.open.append((edge.time, edge.delta))
            self.open = self.open[-MAX_OPEN_EDGES:]
            return None
        step = -edge.delta
        candidates = [
            (abs(power - step), index)
            for index, (_, power) in enumerate(self.open)
            if self.matches(power, step)
        ]
        if not candidates:
            return None
        _, index = min(candidates)
        start, power = self.open.pop(index)
        return Run(start=start, end=edge.time, power=(power + step) / 2)


@dataclass(slots=True)
class Cluster:
    """Runs of similar power and duration, possibly named by the user."""

    id: str
    power: float
    duration_s: float
    count: int = 0
    energy_wh: float = 0.0
    first_seen: str | None = None
    last_seen: str | None = None
    name: str | None = None
    hours: list[int] = field(default_factory=lambda: [0] * 24)
    asked: bool = False

    def matches(self, run: Run) -> bool:
        power_ok = abs(math.log(max(run.power, 1) / max(self.power, 1))) <= CLUSTER_POWER_TOLERANCE
        duration = max(run.duration.total_seconds(), 1)
        duration_ok = abs(math.log(duration / max(self.duration_s, 1))) <= math.log(
            CLUSTER_DURATION_FACTOR
        )
        return power_ok and duration_ok

    def add(self, run: Run) -> None:
        self.count += 1
        weight = 1 / min(self.count, CLUSTER_SMOOTHING)
        self.power += (run.power - self.power) * weight
        self.duration_s += (run.duration.total_seconds() - self.duration_s) * weight
        self.energy_wh += run.energy_wh
        self.last_seen = run.end.isoformat()
        if self.first_seen is None:
            self.first_seen = run.start.isoformat()
        self.hours[run.start.hour] += 1

    @property
    def typical_hour(self) -> int | None:
        if not any(self.hours):
            return None
        return max(range(24), key=lambda hour: self.hours[hour])


class ApplianceCatalog:
    """Assign runs to clusters, keep the list bounded and serialisable."""

    def __init__(self, clusters: Iterable[Cluster] = ()) -> None:
        self.clusters: list[Cluster] = list(clusters)

    def best_match(self, run: Run) -> Cluster | None:
        candidates = [cluster for cluster in self.clusters if cluster.matches(run)]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda c: abs(math.log(max(run.power, 1) / max(c.power, 1))),
        )

    def add(self, run: Run) -> Cluster:
        cluster = self.best_match(run)
        if cluster is None:
            cluster = Cluster(
                id=uuid.uuid4().hex[:8],
                power=run.power,
                duration_s=run.duration.total_seconds(),
            )
            self.clusters.append(cluster)
            self._prune()
        cluster.add(run)
        return cluster

    def _prune(self) -> None:
        """Drop the rarest unnamed clusters when the list grows too long."""
        if len(self.clusters) <= MAX_CLUSTERS:
            return
        unnamed = sorted(
            (c for c in self.clusters if c.name is None), key=lambda c: (c.count, c.last_seen or "")
        )
        for cluster in unnamed[: len(self.clusters) - MAX_CLUSTERS]:
            self.clusters.remove(cluster)

    def get(self, cluster_id: str) -> Cluster | None:
        return next((c for c in self.clusters if c.id == cluster_id), None)

    def named(self) -> list[Cluster]:
        return [c for c in self.clusters if c.name]

    def to_question(self, min_count: int) -> Cluster | None:
        """The most frequent unnamed cluster not asked about yet."""
        candidates = [
            c for c in self.clusters if c.name is None and not c.asked and c.count >= min_count
        ]
        return max(candidates, key=lambda c: c.count) if candidates else None

    def dump(self) -> list[dict[str, Any]]:
        return [asdict(cluster) for cluster in self.clusters]

    @classmethod
    def load(cls, raw: Iterable[dict[str, Any]]) -> ApplianceCatalog:
        clusters = []
        for item in raw:
            try:
                clusters.append(Cluster(**item))
            except TypeError:
                continue
        return cls(clusters)


def describe_duration(seconds: float, language: str) -> str:
    """Short human duration, e.g. "1 min", "2 h"."""
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    unit = "h" if language != "de" else "Std."
    return f"{seconds / 3600:.1f} {unit}"
