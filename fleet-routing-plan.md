# Fleet Route Optimization: Project Brief

## Context for Claude Code

We are a trucking logistics company. We want to build a route optimization system for:

- **~1,000 trucks**
- **~500 locations**
- **Hard requirement:** every truck must return to its starting location (domicile).
- **Business goal:** avoid empty trailers (minimize deadhead / empty miles).
- We have access to ML models and want to use them where they add value.

Use this document as the design spec. Start with the baseline in Section 6, step 1, and build incrementally. Ask before making assumptions about data schemas we haven't provided.

---

## 1. Problem framing

With 1,000 trucks and only 500 locations, this is not a classic "one truck visits many customers" VRP. Trucks outnumber locations, so it behaves like a **truckload dispatch / load-matching problem** with a closed-tour requirement. Model it as a **pickup-and-delivery VRP with time windows, driver domiciles, and backhaul incentives**:

- **Nodes:** 500 locations, each acting as a shipper, a consignee, or both.
- **Demand:** a set of loads, each with origin, destination, pickup window, delivery window, trailer type, and weight/cube. Many loads share the same lanes.
- **Vehicles:** 1,000 trucks, each with a home domicile, trailer type, and driver hours-of-service (HOS) remaining.
- **Closed tours:** each truck's tour starts and ends at its domicile, typically within a multi-day horizon.

### "No empty trailers" is a soft constraint

Treat it as a heavily penalized soft constraint, not a hard one. As a hard constraint it often makes the problem infeasible, because lane imbalances force some trucks to reposition empty.

- Objective: **maximize loaded miles, minimize deadhead miles**.
- Implement as a cost term: `deadhead_miles * c_empty` with a large `c_empty`.
- Report the true empty-mile percentage as a KPI.

---

## 2. Mathematical formulation

### Set-partitioning (tour-based) model — preferred at this scale

- Each **column** is a feasible tour for a truck: a sequence of loads and repositioning moves that starts and ends at the domicile and respects HOS, time windows, and capacity.
- Decision variable: `x_t ∈ {0,1}` — tour `t` is selected.
- **Minimize:** `Σ cost_t · x_t`, where cost includes fuel, driver time, tolls, and the deadhead penalty.
- **Subject to:**
  - Every load is covered exactly once (or at most once, with a penalty for unserved loads).
  - Each truck is used at most once.

### Arc-based alternative

Variables `x_ij^k` (truck `k` drives arc `i→j`) are more intuitive but explode at 1,000 trucks. Use only for small prototypes or validation.

---

## 3. Solution approach (hybrid pipeline)

1. **Decompose.** Cluster by region or hub so each subproblem has ~50–150 locations and a subset of trucks.
2. **Generate tours via column generation.** The pricing subproblem is a resource-constrained shortest path (resources: time, HOS, capacity). A heuristic pricer is usually good enough.
3. **Solve the master problem** as a MIP (Gurobi, CPLEX, HiGHS, or OR-Tools CP-SAT).
4. **Polish with Large Neighborhood Search (LNS)** using destroy-and-repair moves across regions. PyVRP (HGS-based), OR-Tools Routing, or VROOM give strong baselines.
5. **Re-optimize on a rolling horizon.** New loads, delays, and breakdowns arrive constantly — re-solve every 15–60 minutes, warm-starting from the current plan.

---

## 4. Where the ML models fit

ML feeds and accelerates the optimizer; it does not replace it. Exact feasibility and cost accounting stay in the solver.

| Use | What it does |
|---|---|
| **Demand & lane forecasting** | Predicts future loads per lane so trucks can be pre-positioned and backhauls planned before loads are tendered. |
| **Travel time & ETA prediction** | Replaces static distance matrices with time-of-day-aware travel times, plus dwell-time prediction per facility. |
| **Load acceptance / cancellation prediction** | Weights loads by reliability so plans don't depend on loads likely to fall through. |
| **Learned heuristics** | A GNN or attention-based policy proposes initial solutions, promising arcs, or tours to generate — speeds up column generation and LNS. |
| **Repositioning value function** | Estimates the future value of a truck ending a trip at city X (approximate dynamic programming). Best tool for the empty-trailer problem: lets the optimizer accept a slightly less profitable load now for a good backhaul later. |
| **Cost & pricing models** | Predicts spot-market rates for backhaul loads to decide whether to buy outside loads to fill a return leg. |

---

## 5. Data requirements

- **Historical loads:** origin, destination, weight, timestamps, rate
- **Telematics / GPS:** actual transit and dwell times
- **Drivers & equipment:** HOS status, domicile, equipment/trailer type
- **Facility constraints:** dock hours, appointment windows
- **Cost parameters:** fuel, tolls, driver pay, deadhead penalty

---

## 6. Build order

1. **Baseline.** Deterministic model with static distances and a cost function. Solve one region with OR-Tools or PyVRP.
2. **Backtest.** Replay a month of historical dispatch decisions; compare empty-mile %, on-time rate, and cost vs. what dispatchers actually did.
3. **Add ML pieces one at a time.** ETA model first, then demand forecast, then the repositioning value function. Measure the lift from each.
4. **Scale up.** Add decomposition and column generation for the full network.
5. **Simulate before deploying.** Stochastic simulator with random delays, cancellations, and new load arrivals to stress-test the rolling-horizon policy.
6. **Shadow-mode deployment.** Dispatchers see recommendations first; track acceptance and override reasons to discover missing constraints.

---

## 7. KPIs

- Empty / deadhead mile percentage (**primary target**)
- Cost per loaded mile
- On-time pickup and delivery rate
- Truck utilization and driver HOS compliance
- Percentage of tours returning to domicile on schedule
- Solve time (must fit within the re-planning window)

---

## 8. Suggested repo structure

```
fleet-routing/
├── data/                 # raw + processed inputs (loads, locations, trucks)
├── src/
│   ├── data/             # loaders, validation, synthetic data generator
│   ├── model/            # formulation: costs, constraints, tour feasibility
│   ├── solver/           # baseline (OR-Tools/PyVRP), column generation, LNS
│   ├── ml/               # ETA, demand forecast, repositioning value function
│   ├── sim/              # rolling-horizon stochastic simulator
│   └── eval/             # backtesting + KPI reporting
├── tests/
└── notebooks/
```

## 9. First task for Claude Code

1. Create a **synthetic data generator** (500 locations with lat/lon, 1,000 trucks with domiciles, a few thousand loads with time windows) so we can develop before real data is connected.
2. Implement the **baseline solver** for a single region (~100 locations, ~200 trucks) using OR-Tools or PyVRP, with:
   - closed tours (return to domicile),
   - pickup-and-delivery pairing,
   - time windows,
   - deadhead penalty in the cost function.
3. Output a **KPI report** (empty-mile %, cost, on-time %, solve time).
4. Write tests for tour feasibility (returns to domicile, time windows respected, each load served at most once).

### Open questions to confirm with the team
- Full-truckload (FTL) vs. less-than-truckload (LTL)?
- Same-day tours or multi-day tours? What is the max tour length?
- Preferred solver/licensing (Gurobi available, or open-source only)?
- What format is the historical data in?
