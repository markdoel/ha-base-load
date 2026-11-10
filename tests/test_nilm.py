"""Tests for appliance detection."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.base_load.nilm import (
    ApplianceCatalog,
    EventPairer,
    Run,
    SteadyStateDetector,
    describe_duration,
)

TZ = ZoneInfo("Europe/Berlin")
T0 = datetime(2026, 11, 10, 14, 0, tzinfo=TZ)


def signal(segments, step_s=3, noise=(0, 8, -6, 12, -10, 4)):
    """[(seconds, watts), ...] -> samples every step_s with a little noise."""
    samples, t, i = [], T0, 0
    for seconds, watts in segments:
        for _ in range(max(1, seconds // step_s)):
            samples.append((t, watts + noise[i % len(noise)]))
            t += timedelta(seconds=step_s)
            i += 1
    return samples


def run_pipeline(samples, min_step=80):
    detector, pairer = SteadyStateDetector(min_step), EventPairer()
    edges, runs = [], []
    for t, w in samples:
        edge = detector.feed(t, w)
        if edge:
            edges.append(edge)
            run = pairer.feed(edge)
            if run:
                runs.append(run)
    return edges, runs


def test_kettle_like_run():
    edges, runs = run_pipeline(signal([(60, 400), (90, 2400), (60, 400)]))
    assert [round(e.delta, -2) for e in edges] == [2000, -2000]
    assert len(runs) == 1
    assert 1950 < runs[0].power < 2050
    assert timedelta(seconds=85) <= runs[0].duration <= timedelta(seconds=95)


def test_noise_and_single_spikes_are_ignored():
    # One-sample spike of 2 kW and steady noise must not produce edges.
    samples = signal([(60, 400)]) + [(T0 + timedelta(seconds=61), 2400)]
    samples += [(T0 + timedelta(seconds=64 + 3 * i), 400 + (i % 3) * 10) for i in range(20)]
    edges, runs = run_pipeline(samples)
    assert edges == [] and runs == []


def test_small_steps_below_minimum_are_ignored():
    edges, _ = run_pipeline(signal([(60, 400), (60, 450), (60, 400)]))
    assert edges == []


def test_overlapping_appliances_pair_correctly():
    # Fridge 120 W on, kettle 2 kW on, kettle off, fridge off.
    edges, runs = run_pipeline(signal([(60, 300), (60, 420), (45, 2420), (60, 420), (60, 300)]))
    powers = sorted(round(r.power, -1) for r in runs)
    assert powers == [120, 2000]


def test_off_edge_without_on_edge_is_ignored():
    pairer = EventPairer()
    detector = SteadyStateDetector(80)
    runs = []
    for t, w in signal([(60, 1000), (60, 400)]):
        edge = detector.feed(t, w)
        if edge and (run := pairer.feed(edge)):
            runs.append(run)
    assert runs == []


def run(power, seconds, hour=14):
    start = T0.replace(hour=hour)
    return Run(start=start, end=start + timedelta(seconds=seconds), power=power)


def test_catalog_clusters_similar_runs():
    catalog = ApplianceCatalog()
    for power, seconds in [(2300, 60), (2250, 70), (2350, 55), (120, 900), (125, 1100)]:
        catalog.add(run(power, seconds))
    assert len(catalog.clusters) == 2
    big = max(catalog.clusters, key=lambda c: c.power)
    assert big.count == 3 and 2200 < big.power < 2400
    # Same power but much longer: a different appliance.
    catalog.add(run(2300, 3600))
    assert len(catalog.clusters) == 3


def test_catalog_question_and_round_trip():
    catalog = ApplianceCatalog()
    for _ in range(6):
        catalog.add(run(2300, 60, hour=0))
    catalog.add(run(120, 900))
    asked = catalog.to_question(min_count=5)
    assert asked is not None and asked.count == 6
    assert asked.typical_hour == 0
    asked.name = "Dishwasher"
    restored = ApplianceCatalog.load(catalog.dump())
    assert [c.name for c in restored.named()] == ["Dishwasher"]
    assert restored.get(asked.id).energy_wh == asked.energy_wh
    assert ApplianceCatalog.load([{"bogus": 1}]).clusters == []


def test_describe_duration():
    assert describe_duration(45, "de") == "45 s"
    assert describe_duration(600, "de") == "10 min"
    assert describe_duration(7200, "de") == "2.0 Std."
