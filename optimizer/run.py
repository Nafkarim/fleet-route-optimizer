"""End-to-end: load data, run baseline + optimizer, build dashboard output.

CLI:  python -m optimizer.run [--time-limit 45] [--deadhead-penalty 3.5]
Writes output/plan.json and output/details.json and prints the KPIs.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .baseline import naive_dispatch
from .data import load_problem
from .kpis import build_output
from .solver import Optimizer, SolveSettings

OUT_DIR = Path(__file__).resolve().parent.parent / "output"


def run_plan(settings: SolveSettings, progress: Callable[[dict], None] | None = None):
    progress = progress or (lambda info: None)
    progress({"phase": "Loading data", "pct": 1})
    prob = load_problem()
    progress({"phase": "Running naive dispatch for comparison", "pct": 3})
    base = naive_dispatch(prob, settings)
    opt = Optimizer(prob, settings, progress)
    sol = opt.solve()
    progress({"phase": "Preparing dashboard", "pct": 98})
    s = asdict(settings)
    s["deadhead_penalty"] = opt.sim.dhp
    if s["unserved_penalty"] is None:
        s["unserved_penalty"] = prob.params.unserved_penalty
    return build_output(prob, opt, sol, base, s)


def save(summary: dict, detail: dict):
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "plan.json").write_text(json.dumps(summary))
    (OUT_DIR / "details.json").write_text(json.dumps(detail))


def print_kpis(summary: dict):
    k, b = summary["kpis"], summary["baseline"]
    rows = [
        ("Empty-mile %", "empty_pct", "{:.1f}%"),
        ("Loads delivered", "loads_served", "{:,}"),
        ("On-time deliveries", "on_time_pct", "{:.1f}%"),
        ("Operating cost", "operating_cost", "${:,.0f}"),
        ("Cost per loaded mile", "cost_per_loaded_mile", "${:.2f}"),
        ("Revenue", "revenue", "${:,.0f}"),
        ("Margin", "margin", "${:,.0f}"),
        ("Trucks dispatched", "trucks_used", "{:,}"),
        ("Tours home on time", "home_on_time_pct", "{:.1f}%"),
    ]
    print(f"{'KPI':<24}{'Optimized':>16}{'Naive dispatch':>18}")
    for name, key, fmt in rows:
        print(f"{name:<24}{fmt.format(k[key]):>16}{fmt.format(b[key]):>18}")
    print(f"Solve time: {k['solve_seconds']:.1f}s ({k['iterations']:,} improvement rounds)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--time-limit", type=float, default=45)
    ap.add_argument("--deadhead-penalty", type=float, default=None)
    ap.add_argument("--exclude-tentative", action="store_true")
    a = ap.parse_args()
    st = SolveSettings(time_limit=a.time_limit, deadhead_penalty=a.deadhead_penalty,
                       include_tentative=not a.exclude_tentative)
    summary, detail = run_plan(st, lambda info: print(f"  [{info['pct']:5.1f}%] {info['phase']}", flush=True)
                               if info["pct"] < 31 or info["pct"] > 97 else None)
    save(summary, detail)
    print_kpis(summary)
