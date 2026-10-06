"""Independent feasibility checks on a solved plan.

These tests do not trust the simulator's own feasibility flag: they re-check
the event timeline the dashboard displays (stops, legs, driving/rest blocks).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from optimizer.data import load_problem  # noqa: E402
from optimizer.run import run_plan  # noqa: E402
from optimizer.solver import SolveSettings  # noqa: E402
from optimizer.tour import Simulator  # noqa: E402

EPS = 0.6  # minutes; event times are rounded to 0.1 min in the output


@pytest.fixture(scope="module")
def prob():
    return load_problem()


@pytest.fixture(scope="module")
def plan():
    return run_plan(SolveSettings(time_limit=5))


def test_every_load_accounted_for_once(prob, plan):
    summary, detail = plan
    served = [s["load_id"] for d in detail.values() for s in d["stops"]]
    assert len(served) == len(set(served)), "a load is assigned to two trucks"
    uncovered = {u["load_id"] for u in summary["uncovered"]}
    assert not set(served) & uncovered
    assert len(served) + len(uncovered) == len(prob.loads)


def test_tours_start_and_end_at_home(prob, plan):
    _, detail = plan
    trucks = {t.id: t for t in prob.trucks}
    for tid, d in detail.items():
        t = trucks[tid]
        legs = d["leg_list"]
        if not legs:
            assert not d["stops"]
            continue
        assert legs[0]["from"] == t.start, tid
        assert legs[-1]["to"] == t.home, f"{tid} does not return to its domicile"
        for a, b in zip(legs, legs[1:]):
            assert a["to"] == b["from"], f"{tid} teleports between legs"
        assert legs[0]["depart"] >= t.avail - EPS, f"{tid} leaves before it is available"
        assert d["return"] <= t.deadline + EPS, f"{tid} returns after its max tour length"


def test_time_windows_docks_and_equipment(prob, plan):
    _, detail = plan
    loads = {l.id: l for l in prob.loads}
    trucks = {t.id: t for t in prob.trucks}
    for tid, d in detail.items():
        for s in d["stops"]:
            l = loads[s["load_id"]]
            assert l.trailer == trucks[tid].trailer
            assert l.weight <= trucks[tid].capacity_lbs
            assert l.pe - EPS <= s["pickup_at"] <= l.pl + EPS, f"{l.id} picked up outside its window"
            assert s["delivered_at"] >= l.de - EPS, f"{l.id} delivered before its window opens"
            assert s["delivered_at"] <= l.dl + EPS, f"{l.id} delivered late"
            for loc, at in ((l.o, s["pickup_at"]), (l.d, s["delivered_at"])):
                L = prob.locations[loc]
                if L.dock_open == 0 and L.dock_close >= 24:
                    continue
                h = (at % 1440) / 60
                assert L.dock_open - 0.01 <= h < L.dock_close + 0.01, f"{l.id} serviced while dock closed"
        times = [s["pickup_at"] for s in d["stops"]]
        assert times == sorted(times)


def test_hours_of_service(prob, plan):
    _, detail = plan
    pr = prob.params
    trucks = {t.id: t for t in prob.trucks}
    for tid, d in detail.items():
        t = trucks[tid]
        m = 2 if t.team else 1
        ev = d["events"]
        if not ev:
            continue
        for a, b in zip(ev, ev[1:]):
            assert b[1] >= a[2] - EPS, f"{tid} has overlapping events"
        drive_lim, window_lim = t.hos_drive * m, t.hos_duty * m
        shift_drive, shift_start, since_break = 0.0, None, 0.0
        prev_end = t.avail
        for kind, s, e in ev:
            gap = s - prev_end
            off = kind in ("rest", "restart") or (kind == "wait" and e - s >= pr.reset_len - EPS)
            if gap >= pr.reset_len - EPS or off:
                shift_drive, shift_start, since_break = 0.0, None, 0.0
                drive_lim, window_lim = pr.max_drive * m, pr.max_duty * m
                prev_end = e
                if off:
                    continue
            if shift_start is None:
                shift_start = s
            if kind.startswith("drive"):
                shift_drive += e - s
                since_break += e - s
                assert shift_drive <= drive_lim + EPS, f"{tid} drives over the shift limit"
                assert e - shift_start <= window_lim + EPS, f"{tid} drives past the 14-hour window"
                if not t.team:
                    assert since_break <= pr.break_after + EPS, f"{tid} skips the 30-minute break"
            elif e - s >= pr.break_len - EPS:
                since_break = 0.0
            prev_end = e


# ---------------------------------------------------------------- hand-built cases
def _truck(prob, trailer, **kw):
    t = next(t for t in prob.trucks if t.trailer == trailer and not t.team)
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def test_long_haul_forces_rest(prob):
    """A load with more than 11 h of driving must contain a rest (or a long off-duty wait)."""
    sim = Simulator(prob)
    longs = sorted((l for l in prob.loads if l.miles > 800 and l.trailer == "dry_van"), key=lambda l: l.pe)
    for long in longs[:200]:
        for t in prob.trucks:
            if (t.trailer == "dry_van" and not t.team
                    and prob.locations[t.start].city_idx == prob.locations[long.o].city_idx):
                r = sim.simulate(t, [long], record=True)
                if r is not None:
                    assert any(e[0] in ("rest", "restart") or (e[0] == "wait" and e[2] - e[1] >= 600)
                               for e in r.events)
                    return
    pytest.fail("expected at least one solo truck that can run an 800+ mile load")


def test_wrong_trailer_rejected(prob):
    sim = Simulator(prob)
    load = next(l for l in prob.loads if l.trailer == "reefer")
    truck = next(t for t in prob.trucks if t.trailer == "flatbed")
    assert sim.simulate(truck, [load]) is None


def test_missed_pickup_window_rejected(prob):
    sim = Simulator(prob)
    load = next(l for l in prob.loads if l.pl < 24 * 60)
    far = max(prob.trucks, key=lambda t: prob.dist[t.start][load.o] if t.trailer == load.trailer else -1)
    assert prob.dist[far.start][load.o] > 1000
    assert sim.simulate(far, [load]) is None


def test_reversed_order_rejected(prob, plan):
    """Running a planned tour backwards must break its time windows."""
    _, detail = plan
    sim = Simulator(prob)
    loads = {l.id: l for l in prob.loads}
    trucks = {t.id: t for t in prob.trucks}
    for tid, d in detail.items():
        seq = [loads[s["load_id"]] for s in d["stops"]]
        if len(seq) >= 2 and seq[-1].pe > seq[0].pl + 1440:
            assert sim.simulate(trucks[tid], seq) is not None
            assert sim.simulate(trucks[tid], seq[::-1]) is None
            return
    pytest.fail("no multi-load tour found in the plan")
