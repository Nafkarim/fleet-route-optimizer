# Fleet Route Optimizer

Plans one week of work for the whole fleet (1,000 trucks, 500 locations, 5,000 loads in `data/`).
It decides which truck hauls which load, and in what order. The goal is as few empty miles as
possible while every truck still gets home. Results are shown in an interactive dashboard.

**Live demo (read-only):** https://nafkarim.github.io/fleet-route-optimizer/

## Run it

```bash
./run.sh
```

The first run sets up a Python environment (`.venv`). Then it opens http://localhost:8000.
If no saved plan exists in `output/`, the optimizer runs first (about a minute).

Other commands:

```bash
.venv/bin/python -m optimizer.run --time-limit 60      # solve from the command line, print KPIs
.venv/bin/python -m pytest tests -q                    # feasibility tests
```

## Reading the dashboard

| Area | What it shows |
|---|---|
| **KPI cards** | Empty-mile %, loads delivered, on-time %, trucks home on time, cost per loaded mile, margin, trucks used. Each card is compared with a *naive dispatch* plan (every load goes to the nearest free truck). |
| **Network overview** | A map of city-to-city lanes. Blue lines are loaded trips and orange dashed lines are empty ones; thicker means more trips. You can switch between the optimized and naive plans. Charts show empty miles by home base, trucks on the road over the week, and loads by trailer type. |
| **Truck routes** | Search or filter any truck. Click one to see its route on the map, a plain-English summary, a day-by-day schedule (driving, loading, waiting, mandatory rest), and every stop with its time window. |
| **Uncovered loads** | Every load not covered, with the reason in plain language: fleet busy, not worth the empty miles, deadline too tight for a solo driver, or out of reach. |
| **Settings** | Change the empty-mile penalty, the uncovered-load penalty, how late deliveries or returns may be, whether to skip tentative or high-risk loads, and how long to search. Then click **Re-optimize**. |

## Data assistant (optional)

Click **Ask about your data** to chat with an assistant powered by Claude. It answers questions about the current
plan, such as "why are so many loads uncovered?", "walk me through T0681's week" or "which lanes have the most
empty repositioning?". It looks the numbers up with tools that read the plan data. Truck and load IDs in its answers
are links into the dashboard.

To turn it on, create a `.env` file in the project folder with your
[Anthropic API key](https://console.anthropic.com/), then restart the dashboard (`.env` is git-ignored):

```
ANTHROPIC_API_KEY=your-key-here
```

## Rules every plan follows

- Every tour starts at the truck's start location and **ends at its home base**, within `max_tour_days`.
- The trailer type matches the load and the weight fits. One load is on the truck at a time (full truckload).
- Pickup happens inside the pickup window and delivery by the deadline. Docks are only used while they're open.
- Hours of service: 11 h driving and a 14 h duty window per shift, a 30 min break after 8 h of driving, a 10 h reset, and a 70 h cycle with a 34 h restart. Team trucks get doubled shift limits.
- By default nothing is late. You can allow lateness in Settings, and it is charged per hour.

## How it works

`optimizer/tour.py` simulates a truck's tour minute by minute. It is the single source of truth for both feasibility and cost.
`optimizer/solver.py` first builds a plan by cheapest insertion, in pickup order. It then improves the plan with Large Neighborhood
Search: it removes related loads (by region and time, poor routes, or one home base), re-inserts them, and keeps changes
with simulated annealing. The objective is operating cost + empty-mile penalty + waiting and lateness penalties
+ (lost revenue + service penalty) for each uncovered load. That last term is weighted by priority and cancellation risk.

| Path | Purpose |
|---|---|
| `optimizer/data.py` | loads the CSV/JSON inputs |
| `optimizer/tour.py` | tour simulation (windows, docks, HOS, return home) |
| `optimizer/solver.py` | construction + LNS |
| `optimizer/baseline.py` | naive dispatcher for comparison |
| `optimizer/kpis.py` | KPIs, uncovered-load reasons, dashboard JSON |
| `server/app.py` | FastAPI backend (`/api/plan`, `/api/truck/{id}`, `/api/solve`, `/api/status`, `/api/chat`) |
| `server/chat.py` | data assistant: Claude tool loop + plan-query tools |
| `web/` | dashboard (vanilla JS, Leaflet, Chart.js) |
| `tests/` | independent feasibility checks on the solved plan |

## Things the data reveals

- **The fleet can't carry every load.** The median load is about 800 miles, or about 1.5 driver-days. That means 1,000 trucks
  on 3–7 day tours can haul only about 1,900 of the 5,000 loads. The rest are reported with reasons.
- **About 850 loads have delivery deadlines that no solo driver can legally meet.** The windows in the generated data
  ignore the mandatory 10 h rest. Only team trucks can take those loads.
