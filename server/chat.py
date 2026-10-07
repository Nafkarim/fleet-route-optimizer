"""Data assistant: Claude answers questions about the current plan using tools
that query the solved plan (trucks, loads, lanes, cities, uncovered reasons).

The conversation history is kept server-side and only ever appended to, so
prompt caching and thinking-block replay stay valid across turns.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator

import anthropic

MODEL = "claude-opus-5-5"
MAX_TOOL_ROUNDS = 10
ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------- credentials
def load_dotenv():
    """Read KEY=VALUE lines from .env in the project root (no extra dependency)."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def credentials_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or (Path.home() / ".config" / "anthropic").exists())


# ---------------------------------------------------------------------- data access
class PlanData:
    """Read-only views over the dashboard's plan summary + per-truck detail."""

    def __init__(self, summary: dict, detail: dict, problem):
        self.s = summary
        self.d = detail
        self.p = problem
        self.h0 = datetime.fromisoformat(summary["horizon_start"])
        self.locs = summary["locations"]
        self.cities = [c["name"] for c in summary["cities"]]
        self.load_truck: dict[str, tuple[str, dict]] = {}
        for tid, t in detail.items():
            for i, stop in enumerate(t["stops"]):
                self.load_truck[stop["load_id"]] = (tid, {**stop, "stop_number": i + 1})
        self.uncovered = {u["load_id"]: u for u in summary["uncovered"]}
        self.loads = {l.id: l for l in problem.loads}

    # helpers ---------------------------------------------------------
    def when(self, minutes: float | None) -> str | None:
        if minutes is None:
            return None
        return (self.h0 + timedelta(minutes=minutes)).strftime("%a %b %d %H:%M")

    def city_of_loc(self, i: int) -> str:
        return self.cities[self.locs[i]["city"]]

    def match_city(self, q: str | None) -> list[str]:
        if not q:
            return []
        q = q.lower().strip()
        exact = [c for c in self.cities if c.lower() == q or c.lower().split(",")[0] == q]
        return exact or [c for c in self.cities if q in c.lower()]

    # tools -----------------------------------------------------------
    def plan_overview(self) -> dict:
        k, b = self.s["kpis"], self.s["baseline"]
        keys = ["loads_total", "loads_served", "loads_unserved", "loads_excluded", "empty_pct", "miles",
                "empty_miles", "loaded_miles", "operating_cost", "revenue", "margin", "cost_per_loaded_mile",
                "on_time_pct", "late_deliveries", "trucks_used", "trucks_total", "home_on_time_pct",
                "loads_per_truck", "drive_hours"]
        rnd = lambda v: round(v, 2) if isinstance(v, float) else v
        return {
            "planning_week": f"{self.when(0)} to {self.when((self.s['horizon_days'] - 1) * 1440 + 1439)}",
            "optimized_plan": {key: rnd(k[key]) for key in keys},
            "naive_dispatch_plan": {key: rnd(b[key]) for key in keys},
            "solve_seconds": round(k.get("solve_seconds", 0), 1),
            "settings_used": self.s["settings"],
            "uncovered_by_reason": dict(Counter(u["code"] for u in self.s["uncovered"])),
            "loads_by_trailer": self.s["charts"]["by_trailer"],
        }

    def find_trucks(self, trailer=None, home_city=None, status=None, team=None,
                    sort_by="loads", descending=True, limit=10) -> dict:
        rows = self.s["trucks"]
        if trailer:
            rows = [t for t in rows if t["trailer"] == trailer]
        if home_city:
            cs = set(self.match_city(home_city))
            if not cs:
                raise ValueError(f"No home base matches '{home_city}'. Known cities: {', '.join(self.cities)}")
            rows = [t for t in rows if t["home_city"] in cs]
        if status == "dispatched":
            rows = [t for t in rows if t["loads"] > 0]
        elif status == "idle":
            rows = [t for t in rows if t["loads"] == 0]
        if team is not None:
            rows = [t for t in rows if t["team"] == bool(team)]
        keyf = {
            "loads": lambda t: t["loads"], "empty_pct": lambda t: t["empty_pct"], "miles": lambda t: t["miles"],
            "empty_miles": lambda t: t["empty_miles"], "revenue": lambda t: t["revenue"], "cost": lambda t: t["cost"],
            "margin": lambda t: t["revenue"] - t["cost"], "truck_id": lambda t: t["id"],
        }.get(sort_by)
        if keyf is None:
            raise ValueError("sort_by must be one of loads, empty_pct, miles, empty_miles, revenue, cost, margin, truck_id")
        busy = [t for t in rows if t["loads"]]
        if sort_by in ("empty_pct",):
            pool = busy  # idle trucks have no miles; leave them out of empty-share rankings
        else:
            pool = rows
        pool = sorted(pool, key=keyf, reverse=bool(descending))
        limit = max(1, min(int(limit), 50))
        miles = sum(t["miles"] for t in busy)
        return {
            "matching_trucks": len(rows),
            "dispatched": len(busy),
            "stayed_home": len(rows) - len(busy),
            "total_loads_hauled": sum(t["loads"] for t in rows),
            "empty_mile_pct_of_group": round(100 * sum(t["empty_miles"] for t in busy) / miles, 1) if miles else None,
            "trucks": [{
                "truck_id": t["id"], "trailer": t["trailer"], "home_base": t["home_city"], "team_drivers": t["team"],
                "loads": t["loads"], "miles": t["miles"], "empty_miles": t["empty_miles"], "empty_pct": t["empty_pct"],
                "revenue": t["revenue"], "operating_cost": t["cost"], "margin": t["revenue"] - t["cost"],
                "leaves_home": self.when(t["depart"]), "back_home": self.when(t["return"]) if t["loads"] else None,
                "tour_limit_days": t["max_days"],
            } for t in pool[:limit]],
        }

    def truck_details(self, truck_id: str) -> dict:
        tid = truck_id.strip().upper()
        if tid not in self.d:
            raise ValueError(f"Unknown truck '{truck_id}'. Truck IDs look like T0001-T1000.")
        t = self.d[tid]
        time_by_kind = defaultdict(float)
        for kind, a, b in t["events"]:
            time_by_kind[kind] += (b - a) / 60
        return {
            "truck_id": tid, "trailer": t["trailer"], "team_drivers": t["team"], "home": t["home_name"],
            "home_city": t["home_city"], "tour_limit_days": t["max_days"],
            "available_from": self.when(t["avail"]), "must_be_home_by": self.when(t["deadline"]),
            "leaves_home": self.when(t["depart"]), "back_home": self.when(t["return"]) if t["stops"] else None,
            "loads": len(t["stops"]), "miles": t["miles"], "empty_miles": t["empty_miles"],
            "empty_pct": t["empty_pct"], "revenue": t["revenue"], "operating_cost": t["cost"],
            "hours_by_activity": {k: round(v, 1) for k, v in sorted(time_by_kind.items())},
            "rests_10h": t["rests"], "breaks_30min": t["breaks"],
            "hours_of_service_at_start": t["hos_start"],
            "stops": [{
                "stop": i + 1, "load_id": s["load_id"], "commodity": s["commodity"], "weight_lbs": s["weight"],
                "priority": s["priority"], "from": f"{s['origin']} ({s['origin_city']})",
                "to": f"{s['dest']} ({s['dest_city']})", "loaded_miles": s["miles"], "rate": s["rate"],
                "picked_up": self.when(s["pickup_at"]),
                "pickup_window": f"{self.when(s['pickup_window'][0])} - {self.when(s['pickup_window'][1])}",
                "delivered": self.when(s["delivered_at"]), "delivery_deadline": self.when(s["delivery_window"][1]),
                "late_minutes": s["late_min"],
            } for i, s in enumerate(t["stops"])],
            "legs": [{
                "from": self.locs[lg["from"]]["name"], "to": self.locs[lg["to"]]["name"],
                "type": "loaded" if lg["loaded"] else "empty", "miles": lg["miles"],
                "depart": self.when(lg["depart"]), "arrive": self.when(lg["arrive"]), "load_id": lg["load_id"],
            } for lg in t["leg_list"]],
        }

    def find_load(self, load_id: str) -> dict:
        lid = load_id.strip().upper()
        L = self.loads.get(lid)
        if L is None:
            raise ValueError(f"Unknown load '{load_id}'. Load IDs look like LD00001-LD05000.")
        info = {
            "load_id": lid, "lane": f"{L.origin_city} -> {L.dest_city}", "trailer": L.trailer,
            "commodity": L.commodity, "weight_lbs": L.weight, "hazmat": L.hazmat, "loaded_miles": round(L.miles),
            "rate": L.rate, "priority": L.priority, "status": L.status, "cancel_probability": L.cancel_p,
            "pickup_window": f"{self.when(L.pe)} - {self.when(L.pl)}",
            "delivery_window": f"{self.when(L.de)} - {self.when(L.dl)}",
            "origin": self.locs[L.o]["name"], "destination": self.locs[L.d]["name"],
        }
        if lid in self.load_truck:
            tid, stop = self.load_truck[lid]
            info["outcome"] = "delivered"
            info["truck_id"] = tid
            info["stop_number_on_truck"] = stop["stop_number"]
            info["picked_up"] = self.when(stop["pickup_at"])
            info["delivered"] = self.when(stop["delivered_at"])
            info["late_minutes"] = stop["late_min"]
        else:
            u = self.uncovered[lid]
            info["outcome"] = "uncovered"
            info["reason_code"] = u["code"]
            info["reason"] = u["text"]
        return info

    def search_uncovered(self, reason=None, trailer=None, origin_city=None, dest_city=None,
                         priority=None, limit=15) -> dict:
        rows = self.s["uncovered"]
        if reason:
            rows = [u for u in rows if u["code"] == reason]
        if trailer:
            rows = [u for u in rows if u["trailer"] == trailer]
        if priority:
            rows = [u for u in rows if u["priority"] == priority]
        if origin_city:
            cs = set(self.match_city(origin_city))
            rows = [u for u in rows if u["origin_city"] in cs]
        if dest_city:
            cs = set(self.match_city(dest_city))
            rows = [u for u in rows if u["dest_city"] in cs]
        limit = max(1, min(int(limit), 50))
        return {
            "matching_loads": len(rows),
            "by_reason": dict(Counter(u["code"] for u in rows)),
            "by_trailer": dict(Counter(u["trailer"] for u in rows)),
            "by_priority": dict(Counter(u["priority"] for u in rows)),
            "lost_revenue": round(sum(u["rate"] for u in rows)),
            "top_origin_cities": Counter(u["origin_city"] for u in rows).most_common(5),
            "loads": [{
                "load_id": u["load_id"], "lane": f"{u['origin_city']} -> {u['dest_city']}", "trailer": u["trailer"],
                "priority": u["priority"], "miles": u["miles"], "rate": u["rate"],
                "pickup_window": f"{self.when(u['pickup_window'][0])} - {self.when(u['pickup_window'][1])}",
                "reason": u["text"],
            } for u in rows[:limit]],
        }

    def city_stats(self, city: str) -> dict:
        cs = self.match_city(city)
        if not cs:
            raise ValueError(f"No city matches '{city}'. Known cities: {', '.join(self.cities)}")
        name = cs[0]
        ci = self.cities.index(name)
        based = [t for t in self.s["trucks"] if t["home_city"] == name]
        busy = [t for t in based if t["loads"]]
        miles = sum(t["miles"] for t in busy)
        out_loads = [l for l in self.loads.values() if self.locs[l.o]["city"] == ci]
        in_loads = [l for l in self.loads.values() if self.locs[l.d]["city"] == ci]
        served = lambda ls: sum(1 for l in ls if l.id in self.load_truck)
        lanes = [ln for ln in self.s["lanes"] if ln["from"] == ci or ln["to"] == ci]
        fmt = lambda ln: {"lane": f"{self.cities[ln['from']]} -> {self.cities[ln['to']]}",
                          "type": "loaded" if ln["loaded"] else "empty", "trips": ln["count"], "miles": ln["miles"]}
        city_chart = {c["city"]: c for c in self.s["charts"]["empty_by_city"]}.get(name, {})
        return {
            "city": name,
            "trucks_based_here": len(based),
            "trucks_by_trailer": dict(Counter(t["trailer"] for t in based)),
            "dispatched": len(busy),
            "empty_pct_of_trucks_based_here": round(100 * sum(t["empty_miles"] for t in busy) / miles, 1) if miles else None,
            "empty_pct_naive_dispatch": city_chart.get("baseline"),
            "loads_picking_up_here": {"total": len(out_loads), "delivered": served(out_loads),
                                      "uncovered": len(out_loads) - served(out_loads)},
            "loads_delivering_here": {"total": len(in_loads), "delivered": served(in_loads),
                                      "uncovered": len(in_loads) - served(in_loads)},
            "freight_balance_note": ("more freight leaves than arrives, so trucks tend to come back empty"
                                     if len(out_loads) > 1.15 * len(in_loads) else
                                     "more freight arrives than leaves, so trucks tend to leave empty"
                                     if len(in_loads) > 1.15 * len(out_loads) else
                                     "inbound and outbound freight are roughly balanced"),
            "busiest_lanes": [fmt(ln) for ln in sorted(lanes, key=lambda x: -x["count"])[:10]],
        }

    def lane_stats(self, origin_city=None, dest_city=None, leg_type="all", plan="optimized", limit=15) -> dict:
        lanes = self.s["lanes"] if plan == "optimized" else self.s["baseline_lanes"]
        if leg_type == "loaded":
            lanes = [l for l in lanes if l["loaded"]]
        elif leg_type == "empty":
            lanes = [l for l in lanes if not l["loaded"]]
        if origin_city:
            cs = {self.cities.index(c) for c in self.match_city(origin_city)}
            lanes = [l for l in lanes if l["from"] in cs]
        if dest_city:
            cs = {self.cities.index(c) for c in self.match_city(dest_city)}
            lanes = [l for l in lanes if l["to"] in cs]
        lanes = sorted(lanes, key=lambda l: -l["count"])
        limit = max(1, min(int(limit), 50))
        return {
            "plan": plan, "matching_lanes": len(lanes),
            "total_trips": sum(l["count"] for l in lanes), "total_miles": sum(l["miles"] for l in lanes),
            "lanes": [{"lane": f"{self.cities[l['from']]} -> {self.cities[l['to']]}",
                       "type": "loaded" if l["loaded"] else "empty", "trips": l["count"], "miles": l["miles"]}
                      for l in lanes[:limit]],
            "note": "City-to-city only; short moves within the same metro are not counted as lanes.",
        }


# ---------------------------------------------------------------------- tool schemas
TRAILERS = ["dry_van", "reefer", "flatbed"]
REASONS = ["fleet_busy", "not_worth_it", "too_tight", "unreachable", "excluded"]


def _tool(name, description, props, required=()):
    return {
        "name": name, "description": description, "eager_input_streaming": True,
        "input_schema": {"type": "object", "properties": props, "required": list(required),
                         "additionalProperties": False},
    }


TOOLS = [
    _tool("plan_overview",
          "Headline KPIs for the optimized plan and the naive-dispatch comparison, the settings used, "
          "uncovered-load counts by reason, and loads by trailer type. Call this first for big-picture questions.",
          {}),
    _tool("find_trucks",
          "Filter, rank and summarise trucks. Returns group totals plus the top trucks by the chosen sort. "
          "Use for questions like 'which trucks have the most empty miles' or 'how many reefers in Dallas stayed home'.",
          {"trailer": {"type": "string", "enum": TRAILERS},
           "home_city": {"type": "string", "description": "Home base city, e.g. 'Houston' or 'Houston, TX'"},
           "status": {"type": "string", "enum": ["dispatched", "idle"]},
           "team": {"type": "boolean", "description": "true = team-driver trucks only, false = solo only"},
           "sort_by": {"type": "string", "enum": ["loads", "empty_pct", "miles", "empty_miles", "revenue", "cost",
                                                  "margin", "truck_id"]},
           "descending": {"type": "boolean"},
           "limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
    _tool("truck_details",
          "Full week for one truck: every stop with times and windows, every loaded/empty leg, hours spent "
          "driving/waiting/resting, and its hours-of-service status at the start.",
          {"truck_id": {"type": "string", "description": "e.g. T0681"}}, ["truck_id"]),
    _tool("find_load",
          "Look up one load: lane, windows, rate, priority, and whether it was delivered (by which truck, when) "
          "or left uncovered (and why).",
          {"load_id": {"type": "string", "description": "e.g. LD00321"}}, ["load_id"]),
    _tool("search_uncovered",
          "Search loads the plan does not cover, with counts by reason/trailer/priority and lost revenue. "
          "Reason codes: fleet_busy (nearby trucks already busy), not_worth_it (empty miles cost more than the "
          "load is worth), too_tight (deadline impossible for a solo driver under HOS), unreachable (no suitable "
          "truck can make the pickup and get home), excluded (removed by settings).",
          {"reason": {"type": "string", "enum": REASONS},
           "trailer": {"type": "string", "enum": TRAILERS},
           "origin_city": {"type": "string"}, "dest_city": {"type": "string"},
           "priority": {"type": "string", "enum": ["critical", "high", "standard"]},
           "limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
    _tool("city_stats",
          "Everything about one metro: trucks based there, their empty-mile share (optimized vs naive), "
          "freight picking up / delivering there, inbound vs outbound balance, and busiest lanes.",
          {"city": {"type": "string", "description": "e.g. 'Laredo' or 'Laredo, TX'"}}, ["city"]),
    _tool("lane_stats",
          "City-to-city lane traffic (trip counts and miles) for loaded or empty legs, in the optimized or "
          "naive plan. Use to find the biggest empty repositioning flows or compare plans on a lane.",
          {"origin_city": {"type": "string"}, "dest_city": {"type": "string"},
           "leg_type": {"type": "string", "enum": ["all", "loaded", "empty"]},
           "plan": {"type": "string", "enum": ["optimized", "naive"]},
           "limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
]

# allowed argument names and types per tool, used to validate eagerly-streamed inputs
_ARG_TYPES = {
    t["name"]: {k: {"string": str, "integer": int, "boolean": bool}[v["type"]]
                for k, v in t["input_schema"]["properties"].items()}
    for t in TOOLS
}


def _validate(name: str, args) -> dict:
    if name not in _ARG_TYPES:
        raise ValueError(f"Unknown tool {name}")
    if not isinstance(args, dict):
        raise ValueError("Tool input must be a JSON object")
    spec = _ARG_TYPES[name]
    for k, v in args.items():
        if k not in spec:
            raise ValueError(f"Unexpected argument '{k}'")
        want = spec[k]
        if want is int and isinstance(v, bool) or not isinstance(v, want):
            raise ValueError(f"Argument '{k}' must be {want.__name__}")
    for req in next(t for t in TOOLS if t["name"] == name)["input_schema"]["required"]:
        if req not in args:
            raise ValueError(f"Missing required argument '{req}'")
    return args


def tool_label(name: str, args: dict) -> str:
    return {
        "plan_overview": "Reading the plan's headline numbers",
        "find_trucks": "Searching trucks" + (f" in {args['home_city']}" if args.get("home_city") else ""),
        "truck_details": f"Opening truck {args.get('truck_id', '')}",
        "find_load": f"Looking up load {args.get('load_id', '')}",
        "search_uncovered": "Searching uncovered loads",
        "city_stats": f"Analyzing {args.get('city', 'city')}",
        "lane_stats": "Analyzing lanes",
    }.get(name, name)


# ---------------------------------------------------------------------- prompt
SYSTEM_TEMPLATE = """You are the data assistant inside a fleet route optimizer dashboard used by a trucking company's \
planners, dispatchers and managers. Help them make sense of this week's plan and answer their questions accurately.

## What the data is
- One planning week. 1,000 trucks, each with a home base (domicile) terminal, a trailer type (dry_van, reefer, \
flatbed), a maximum tour length (3, 5 or 7 days), and hours-of-service (HOS) hours left at the start of the week. \
Some trucks have team drivers.
- 5,000 loads (full truckload). Each has an origin and destination, a pickup window, a delivery window, a trailer \
type, a rate (revenue), a priority (critical/high/standard), a status (tendered/accepted/tentative) and a \
cancellation probability.
- The optimizer chose which truck hauls which load, in what order. Every tour must start and end at the truck's \
home base within its tour limit, use the right trailer, meet pickup and delivery windows and dock hours, and follow \
HOS rules: 11 h driving and a 14 h duty window per shift, a 30 min break after 8 h driving, a 10 h rest between \
shifts, and a 70 h cycle.
- The objective was to minimize operating cost plus a penalty per empty ("deadhead") mile, plus lost revenue and a \
service penalty for each uncovered load.
- "Naive dispatch" is a comparison plan in which each load goes to the nearest free truck. Use it to show what the \
optimizer gained.

## Current plan snapshot
{snapshot}

## How to answer
- Use the tools for any specific number, truck, load, city or lane. Never invent or estimate figures the tools can \
give you. If the data can't answer something, say so plainly and suggest what would.
- The readers are not optimization experts. Lead with the direct answer in one or two sentences, then the \
supporting numbers. Explain jargon briefly the first time (for example, deadhead means driving with an empty \
trailer). Use short markdown: bold for key numbers, bullet lists, and small tables when comparing several items. \
Keep most answers under about 200 words unless the user asks for depth.
- Write truck IDs (T0681) and load IDs (LD00321) exactly like that. The dashboard turns them into links.
- When asked "why", explain the mechanism using the data, such as freight imbalance between cities, tight delivery \
windows versus HOS, tour-length limits or the empty-mile penalty. When it helps, point to the dashboard view that \
shows it: the Network overview, Truck routes or Uncovered loads tabs, or Settings.
- You can recommend Settings changes (empty-mile penalty, penalty for uncovered loads, allowed lateness, tentative \
loads, cancellation-risk cutoff, search time) and explain the likely trade-off. You cannot change settings or \
re-run the optimizer yourself. The user does that with Settings & re-optimize.
- Money is in US dollars. Times are local to the planning week (for example "Tue Oct 13 08:30").
"""


def build_snapshot(data: PlanData) -> str:
    k, b, s = data.s["kpis"], data.s["baseline"], data.s["settings"]
    reasons = Counter(u["code"] for u in data.s["uncovered"])
    return (
        f"- Week: {data.when(0)} to {data.when((data.s['horizon_days'] - 1) * 1440 + 1439)}\n"
        f"- Optimized: {k['loads_served']:,} of {k['loads_total']:,} loads delivered; empty miles {k['empty_pct']:.1f}% "
        f"({k['empty_miles']:,.0f} of {k['miles']:,.0f} mi); cost per loaded mile ${k['cost_per_loaded_mile']:.2f}; "
        f"revenue ${k['revenue']:,.0f}; operating cost ${k['operating_cost']:,.0f}; margin ${k['margin']:,.0f}; "
        f"{k['trucks_used']} of {k['trucks_total']} trucks dispatched; on-time {k['on_time_pct']:.1f}%; "
        f"home on time {k['home_on_time_pct']:.1f}%\n"
        f"- Naive dispatch: {b['loads_served']:,} loads; empty miles {b['empty_pct']:.1f}%; cost per loaded mile "
        f"${b['cost_per_loaded_mile']:.2f}; margin ${b['margin']:,.0f}; {b['trucks_used']} trucks dispatched\n"
        f"- Uncovered loads by reason: {dict(reasons)}\n"
        f"- Settings: empty-mile penalty ${s['deadhead_penalty']}/mi, uncovered penalty ${s['unserved_penalty']}, "
        f"late delivery allowed {s.get('late_delivery_allow_h', 0)} h, late return allowed "
        f"{s.get('late_return_allow_h', 0)} h, include tentative {s['include_tentative']}, "
        f"max cancel probability {s['max_cancel_prob']}, search time {s['time_limit']} s"
    )


# ---------------------------------------------------------------------- conversations
class Conversation:
    def __init__(self, data: PlanData, plan_id: str):
        self.id = uuid.uuid4().hex
        self.plan_id = plan_id
        self.system = SYSTEM_TEMPLATE.format(snapshot=build_snapshot(data))
        self.messages: list = []
        self.lock = threading.Lock()


class ChatService:
    def __init__(self, get_data: Callable[[], tuple[PlanData, str]]):
        self.get_data = get_data
        self.convs: dict[str, Conversation] = {}
        self._client = None

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def _conversation(self, conv_id: str | None) -> tuple[Conversation, PlanData, bool]:
        data, plan_id = self.get_data()
        conv = self.convs.get(conv_id or "")
        reset = False
        if conv is None or conv.plan_id != plan_id:
            reset = conv is not None
            conv = Conversation(data, plan_id)
            self.convs[conv.id] = conv
            if len(self.convs) > 200:  # keep memory bounded
                self.convs.pop(next(iter(self.convs)))
        return conv, data, reset

    def run_tool(self, data: PlanData, name: str, args: dict) -> str:
        fn = getattr(data, name)
        return json.dumps(fn(**args), default=str)

    def stream(self, conv_id: str | None, user_text: str) -> Iterator[dict]:
        conv, data, reset = self._conversation(conv_id)
        yield {"type": "conversation", "id": conv.id, "reset": reset}
        if not conv.lock.acquire(blocking=False):
            yield {"type": "error", "message": "Still answering the previous question - one moment."}
            return
        try:
            conv.messages.append({"role": "user", "content": user_text})
            yield from self._loop(conv, data)
        except anthropic.AuthenticationError:
            yield {"type": "error", "message": "The Anthropic API key was rejected. Check ANTHROPIC_API_KEY in .env."}
        except anthropic.PermissionDeniedError:
            yield {"type": "error", "message": "This API key doesn't have access to the model."}
        except anthropic.RateLimitError:
            yield {"type": "error", "message": "Rate limited by the API. Wait a few seconds and try again."}
        except anthropic.APIStatusError as e:
            yield {"type": "error", "message": f"The AI service returned an error ({e.status_code}). Try again."}
        except anthropic.APIConnectionError:
            yield {"type": "error", "message": "Couldn't reach the AI service. Check the internet connection."}
        finally:
            conv.lock.release()

    def _loop(self, conv: Conversation, data: PlanData) -> Iterator[dict]:
        json_retries = 0
        rounds = 0
        while rounds < MAX_TOOL_ROUNDS:
            try:
                with self.client.beta.messages.stream(
                    model=MODEL,
                    max_tokens=16000,
                    system=conv.system,
                    tools=TOOLS,
                    messages=conv.messages,
                    output_config={"effort": "medium"},
                    cache_control={"type": "ephemeral"},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                ) as stream:
                    for event in stream:
                        if event.type == "text":
                            yield {"type": "text", "text": event.text}
                    response = stream.get_final_message()
                json_retries = 0
            except ValueError:
                # tool input JSON the SDK could not parse at all: re-issue the turn (bounded)
                json_retries += 1
                if json_retries > 2:
                    raise
                continue

            conv.messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "refusal":
                yield {"type": "text", "text": "\n\nSorry, I can't help with that request."}
                return

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                if response.stop_reason == "max_tokens":
                    yield {"type": "text", "text": "\n\n*(answer cut short - ask me to continue)*"}
                return

            results = []
            for block in tool_uses:
                if response.stop_reason == "max_tokens":
                    results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                    "content": "Tool input was cut off; call the tool again."})
                    continue
                try:
                    args = _validate(block.name, block.input)
                    yield {"type": "tool", "label": tool_label(block.name, args)}
                    out = self.run_tool(data, block.name, args)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
                except (ValueError, TypeError) as e:
                    results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                    "content": str(e)})
            conv.messages.append({"role": "user", "content": results})
            rounds += 1
        yield {"type": "text", "text": "\n\n*(stopped after many lookups - try a narrower question)*"}
