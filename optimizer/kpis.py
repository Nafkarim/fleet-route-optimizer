"""Turn a solved plan into KPIs and JSON-ready data for the dashboard."""
from __future__ import annotations

from dataclasses import replace

from .data import Problem
from .solver import Optimizer, Solution
from .tour import Simulator, TourResult


def _tours(prob: Problem, sol: Solution, sim: Simulator) -> list[TourResult]:
    out = []
    for k, seq in enumerate(sol.seqs):
        r = sim.simulate(prob.trucks[k], seq, record=True)
        if r is None:
            raise RuntimeError(f"infeasible tour in solution for truck {prob.trucks[k].id}")
        out.append(r)
    return out


def _op_cost(truck, r: TourResult, prob: Problem) -> float:
    """Real dollars (no deadhead penalty): miles, waiting, lateness."""
    pr = prob.params
    return (r.miles * truck.cost_per_mile + r.wait / 60 * pr.detention_per_hour
            + r.late_delivery / 60 * pr.late_delivery_per_hour + r.late_return / 60 * pr.late_return_per_hour)


def kpis(prob: Problem, sol: Solution, tours: list[TourResult]) -> dict:
    trucks = prob.trucks
    served = [l for seq in sol.seqs for l in seq]
    miles = sum(r.miles for r in tours)
    empty = sum(r.empty_miles for r in tours)
    loaded = sum(r.loaded_miles for r in tours)
    op = sum(_op_cost(trucks[k], r, prob) for k, r in enumerate(tours))
    revenue = sum(l.rate for l in served)
    used = [k for k, s in enumerate(sol.seqs) if s]
    late_loads = 0
    for k in used:
        ev = [e for e in tours[k].events if e[0] == "unload"]
        for e, l in zip(ev, sol.seqs[k]):
            if e[1] > l.dl + 1e-6:
                late_loads += 1
    home_on_time = sum(1 for k in used if tours[k].late_return <= 1e-6)
    active = len(prob.loads) - len(sol.excluded)
    drive_h = sum(e[2] - e[1] for r in tours for e in r.events if e[0].startswith("drive")) / 60
    return {
        "loads_total": active,
        "loads_served": len(served),
        "loads_unserved": len(sol.unserved),
        "loads_excluded": len(sol.excluded),
        "served_pct": 100 * len(served) / max(1, active),
        "miles": miles,
        "empty_miles": empty,
        "loaded_miles": loaded,
        "empty_pct": 100 * empty / max(1, miles),
        "operating_cost": op,
        "revenue": revenue,
        "margin": revenue - op,
        "cost_per_loaded_mile": op / max(1, loaded),
        "on_time_pct": 100 * (len(served) - late_loads) / max(1, len(served)),
        "late_deliveries": late_loads,
        "trucks_used": len(used),
        "trucks_total": len(trucks),
        "home_on_time_pct": 100 * home_on_time / max(1, len(used)),
        "loads_per_truck": len(served) / max(1, len(used)),
        "drive_hours": drive_h,
        "objective": sol.objective,
    }


def _merge_events(events):
    out = []
    for kind, s, e in events:
        if e - s < 1e-6:
            continue
        if out and out[-1][0] == kind and abs(out[-1][2] - s) < 1e-6:
            out[-1][2] = e
        else:
            out.append([kind, s, e])
    return [[k, round(s, 1), round(e, 1)] for k, s, e in out]


def unserved_reasons(prob: Problem, opt: Optimizer, unserved: set[int]) -> dict[int, dict]:
    """Explain, in plain language, why each uncovered load was left uncovered."""
    sim = opt.sim
    by_home_city: dict[tuple, list] = {}
    for t in prob.trucks:
        by_home_city.setdefault((t.trailer, prob.locations[t.home].city_idx), []).append(t)
    out = {}
    for i in unserved:
        L = prob.loads[i]
        delta, k, _ = opt.best_insertion(L)
        pen = opt.penalty[i]
        trailer = L.trailer.replace("_", " ")
        if k >= 0:
            out[i] = {"code": "not_worth_it",
                      "text": f"Covering it would add ${delta:,.0f} in cost, mostly empty miles. "
                              f"Leaving it uncovered costs ${pen:,.0f}.",
                      "extra_cost": round(delta)}
            continue
        city = prob.locations[L.o].city_idx
        alone = False
        for c in prob.city_near[city]:
            for t in by_home_city.get((L.trailer, c), []):
                if sim.simulate(t, [L]) is not None:
                    alone = True
                    break
            if alone:
                break
        if alone:
            out[i] = {"code": "fleet_busy",
                      "text": f"Every nearby {trailer} truck that could make it is already hauling other loads then."}
        else:
            # Would a fully rested solo driver parked at the shipper make it? If not, the
            # delivery deadline itself is shorter than legal driving + loading allows.
            pr = prob.params
            ideal = replace(prob.trucks[0], start=L.o, home=L.d, trailer=L.trailer, team=False,
                            capacity_lbs=10**6, hos_drive=pr.max_drive, hos_duty=pr.max_duty,
                            hos_cycle=pr.cycle_limit, avail=L.pe, deadline=L.dl + 10 * 1440)
            if sim.simulate(ideal, [L]) is None:
                drive_h = prob.drive[L.o][L.d] / 60
                out[i] = {"code": "too_tight",
                          "text": f"Deadline too tight to drive legally: {drive_h:.1f} h of driving plus loading "
                                  f"needs a mandatory rest, pushing delivery past the deadline. "
                                  f"Only a team truck could make it."}
            else:
                out[i] = {"code": "unreachable",
                          "text": f"No {trailer} truck based near {L.origin_city} can make the pickup window "
                                  f"and still get home within its tour limit."}
    return out


def build_output(prob: Problem, opt: Optimizer, sol: Solution, base: Solution, settings: dict) -> tuple[dict, dict]:
    """Returns (summary for the dashboard, per-truck detail keyed by truck id)."""
    sim = Simulator(prob, opt.sim.dhp)
    tours = _tours(prob, sol, sim)
    btours = _tours(prob, base, sim)
    k_opt = kpis(prob, sol, tours)
    k_base = kpis(prob, base, btours)
    k_opt["solve_seconds"] = sol.seconds
    k_opt["iterations"] = sol.iterations
    hstart = prob.params.horizon_start
    locs = prob.locations

    # city centroids for the aggregated lane map
    cxy: dict[int, list] = {}
    for l in locs:
        c = cxy.setdefault(l.city_idx, [0.0, 0.0, 0])
        c[0] += l.lat
        c[1] += l.lon
        c[2] += 1
    cities = [{"name": prob.cities[c], "lat": v[0] / v[2], "lon": v[1] / v[2]} for c, v in sorted(cxy.items())]

    def lanes(ts):
        agg: dict[tuple, list] = {}
        for r in ts:
            for fr, to, loaded, mi, *_ in r.legs:
                a, b = locs[fr].city_idx, locs[to].city_idx
                if a == b:
                    continue
                v = agg.setdefault((a, b, loaded), [0, 0.0])
                v[0] += 1
                v[1] += mi
        return [{"from": a, "to": b, "loaded": ld, "count": v[0], "miles": round(v[1])}
                for (a, b, ld), v in agg.items()]

    truck_rows, detail = [], {}
    for k, (t, r) in enumerate(zip(prob.trucks, tours)):
        seq = sol.seqs[k]
        op = _op_cost(t, r, prob)
        rev = sum(l.rate for l in seq)
        row = {
            "id": t.id, "trailer": t.trailer, "home": t.home, "home_city": prob.cities[locs[t.home].city_idx],
            "team": t.team, "loads": len(seq), "miles": round(r.miles), "empty_miles": round(r.empty_miles),
            "empty_pct": round(100 * r.empty_miles / r.miles, 1) if r.miles else 0.0,
            "cost": round(op), "revenue": round(rev),
            "depart": round(r.legs[0][4]) if r.legs else None, "return": round(r.return_time),
            "deadline": round(t.deadline), "avail": round(t.avail), "max_days": t.max_days,
            "late_return_h": round(r.late_return / 60, 1),
            "legs": [[fr, to, 1 if ld else 0] for fr, to, ld, *_ in r.legs],
        }
        truck_rows.append(row)

        loads_ev = [e for e in r.events if e[0] == "load"]
        unload_ev = [e for e in r.events if e[0] == "unload"]
        stops = []
        for l, le, ue in zip(seq, loads_ev, unload_ev):
            stops.append({
                "load_id": l.id, "commodity": l.commodity, "weight": l.weight, "priority": l.priority,
                "origin": locs[l.o].name, "origin_city": l.origin_city,
                "dest": locs[l.d].name, "dest_city": l.dest_city,
                "pickup_window": [round(l.pe), round(l.pl)], "pickup_at": round(le[1]),
                "delivery_window": [round(l.de), round(l.dl)], "delivered_at": round(ue[1]),
                "late_min": round(max(0.0, ue[1] - l.dl)), "miles": round(l.miles), "rate": round(l.rate),
            })
        legs = [{"from": fr, "to": to, "loaded": ld, "miles": round(mi), "depart": round(dp),
                 "arrive": round(ar), "load_id": lid} for fr, to, ld, mi, dp, ar, lid in r.legs]
        drive_h = sum(e[2] - e[1] for e in r.events if e[0].startswith("drive")) / 60
        detail[t.id] = {
            **row, "events": _merge_events(r.events), "stops": stops, "leg_list": legs,
            "home_name": locs[t.home].name, "drive_hours": round(drive_h, 1),
            "rests": sum(1 for e in r.events if e[0] == "rest"),
            "breaks": sum(1 for e in r.events if e[0] == "break"),
            "hos_start": {"drive_h": round(t.hos_drive / 60, 1), "duty_h": round(t.hos_duty / 60, 1),
                          "cycle_h": round(t.hos_cycle / 60, 1)},
        }

    reasons = unserved_reasons(prob, opt, sol.unserved)
    uncovered = []
    for i in sorted(sol.unserved, key=lambda i: prob.loads[i].pe):
        l = prob.loads[i]
        uncovered.append({
            "load_id": l.id, "origin": l.o, "dest": l.d, "origin_city": l.origin_city, "dest_city": l.dest_city,
            "trailer": l.trailer, "priority": l.priority, "miles": round(l.miles), "rate": round(l.rate),
            "pickup_window": [round(l.pe), round(l.pl)], "status": l.status, **reasons[i],
        })
    for i in sorted(sol.excluded, key=lambda i: prob.loads[i].pe):
        l = prob.loads[i]
        uncovered.append({
            "load_id": l.id, "origin": l.o, "dest": l.d, "origin_city": l.origin_city, "dest_city": l.dest_city,
            "trailer": l.trailer, "priority": l.priority, "miles": round(l.miles), "rate": round(l.rate),
            "pickup_window": [round(l.pe), round(l.pl)], "status": l.status, "code": "excluded",
            "text": "Left out by your settings (tentative load or high cancellation risk).",
        })

    # charts ----------------------------------------------------------
    def empty_by_city(ts, s):
        agg: dict[str, list] = {}
        for k, r in enumerate(ts):
            if not s.seqs[k]:
                continue
            c = prob.cities[locs[prob.trucks[k].home].city_idx]
            v = agg.setdefault(c, [0.0, 0.0])
            v[0] += r.empty_miles
            v[1] += r.miles
        return {c: 100 * v[0] / v[1] for c, v in agg.items() if v[1]}

    eo, eb = empty_by_city(tours, sol), empty_by_city(btours, base)
    city_chart = sorted(({"city": c, "optimized": round(eo.get(c, 0), 1), "baseline": round(eb.get(c, 0), 1)}
                         for c in set(eo) | set(eb)), key=lambda d: -d["optimized"])

    by_trailer = {}
    for l in prob.loads:
        by_trailer.setdefault(l.trailer, {"trailer": l.trailer, "served": 0, "uncovered": 0, "excluded": 0})
    for seq in sol.seqs:
        for l in seq:
            by_trailer[l.trailer]["served"] += 1
    for i in sol.unserved:
        by_trailer[prob.loads[i].trailer]["uncovered"] += 1
    for i in sol.excluded:
        by_trailer[prob.loads[i].trailer]["excluded"] += 1

    horizon_min = (prob.params.horizon_days + 3) * 1440
    step = 120
    nb = int(horizon_min // step)

    def on_road(ts):
        counts = [0] * nb
        for r in ts:
            if not r.legs:
                continue
            s, e = r.legs[0][4], r.return_time
            for b in range(max(0, int(s // step)), min(nb, int(e // step) + 1)):
                counts[b] += 1
        return counts

    def loaded_now(ts):
        counts = [0] * nb
        for r in ts:
            for kind, s, e in r.events:
                if kind == "drive_loaded":
                    for b in range(max(0, int(s // step)), min(nb, int(e // step) + 1)):
                        counts[b] += 1
        return counts

    summary = {
        "horizon_start": hstart.isoformat(),
        "horizon_days": prob.params.horizon_days,
        "settings": settings,
        "kpis": k_opt,
        "baseline": k_base,
        "cities": cities,
        "locations": [{"i": l.idx, "id": l.id, "name": l.name, "city": l.city_idx, "lat": l.lat, "lon": l.lon,
                       "type": l.type} for l in locs],
        "lanes": lanes(tours),
        "baseline_lanes": lanes(btours),
        "trucks": truck_rows,
        "uncovered": uncovered,
        "charts": {
            "empty_by_city": city_chart,
            "by_trailer": list(by_trailer.values()),
            "fleet_time": {"step_min": step, "on_road": on_road(tours), "hauling": loaded_now(tours),
                           "baseline_on_road": on_road(btours)},
            "history": [[round(s, 1), round(o)] for s, o in sol.history],
        },
    }
    return summary, detail
