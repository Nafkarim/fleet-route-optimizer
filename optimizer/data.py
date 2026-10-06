"""Load the CSV/JSON inputs in data/ into fast in-memory structures.

All times are converted to integer-ish minutes since the planning horizon start
(Monday 00:00), so minute % 1440 is the time of day.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

PRIORITY_WEIGHT = {"standard": 1.0, "high": 1.4, "critical": 2.0}


@dataclass
class Location:
    idx: int
    id: str
    name: str
    city: str
    state: str
    lat: float
    lon: float
    type: str
    dock_open: int   # hour of day
    dock_close: int  # hour of day
    dwell: int       # minutes per stop
    city_idx: int = -1


@dataclass
class Truck:
    idx: int
    id: str
    driver_id: str
    home: int          # domicile location idx
    start: int         # start location idx
    trailer: str
    capacity_lbs: int
    team: bool
    hos_drive: float   # minutes remaining at start
    hos_duty: float
    hos_cycle: float
    avail: float       # minutes since horizon start
    max_days: int
    deadline: float    # latest scheduled return (avail + max_days)
    cost_per_mile: float  # all-in operating cost incl. fuel, driver pay, tolls


@dataclass
class Load:
    idx: int
    id: str
    o: int
    d: int
    origin_city: str
    dest_city: str
    trailer: str
    commodity: str
    weight: int
    hazmat: bool
    pe: float  # pickup earliest (min)
    pl: float  # pickup latest
    de: float  # delivery earliest
    dl: float  # delivery latest
    miles: float
    rate: float
    priority: str
    status: str
    cancel_p: float


@dataclass
class Params:
    horizon_start: datetime
    horizon_days: int
    fuel_price: float
    driver_pay_per_mile: float
    detention_per_hour: float
    deadhead_penalty: float
    unserved_penalty: float
    late_delivery_per_hour: float
    late_return_per_hour: float
    toll_per_mile: float
    max_drive: float        # minutes per shift
    max_duty: float         # minutes per shift (14 h window)
    break_after: float      # minutes of driving before a 30-min break
    break_len: float
    reset_len: float        # 10-h off-duty reset
    cycle_limit: float      # 70 h
    restart_len: float = 34 * 60
    late_delivery_allow: float = 4 * 60   # deliveries may be up to 4 h late (penalised)
    late_return_allow: float = 12 * 60    # trucks may get home up to 12 h late (penalised)


@dataclass
class Problem:
    locations: list[Location]
    trucks: list[Truck]
    loads: list[Load]
    params: Params
    dist: list[list[float]]    # road miles
    drive: list[list[float]]   # drive minutes
    cities: list[str]
    city_near: list[list[int]] = field(default_factory=list)  # city idx -> nearby city idxs
    loc_index: dict[str, int] = field(default_factory=dict)


def _minutes(ts: str, start: datetime) -> float:
    return (datetime.fromisoformat(ts) - start).total_seconds() / 60.0


def _bool(v) -> bool:
    return str(v).strip().lower() == "true"


def load_problem(data_dir: Path = DATA_DIR) -> Problem:
    cp = json.loads((data_dir / "cost_params.json").read_text())
    hos = cp["hos_rules"]
    start = datetime.fromisoformat(cp["horizon_start"])
    params = Params(
        horizon_start=start,
        horizon_days=cp["horizon_days"],
        fuel_price=cp["fuel_price_per_gallon_usd"],
        driver_pay_per_mile=cp["driver_pay_per_mile_usd"],
        detention_per_hour=cp["driver_pay_per_hour_detention_usd"],
        deadhead_penalty=cp["deadhead_penalty_per_mile_usd"],
        unserved_penalty=cp["unserved_load_penalty_usd"],
        late_delivery_per_hour=cp["late_delivery_penalty_per_hour_usd"],
        late_return_per_hour=cp["late_return_to_domicile_penalty_per_hour_usd"],
        toll_per_mile=cp["toll_estimate_per_mile_usd"],
        max_drive=hos["max_drive_hours_per_shift"] * 60,
        max_duty=hos["max_duty_hours_per_shift"] * 60,
        break_after=hos["required_break_after_drive_hours"] * 60,
        break_len=hos["break_minutes"],
        reset_len=hos["off_duty_reset_hours"] * 60,
        cycle_limit=hos["cycle_limit_hours"] * 60,
    )

    ldf = pd.read_csv(data_dir / "locations.csv")
    cities = sorted({f"{r.city}, {r.state}" for r in ldf.itertuples()})
    city_idx = {c: i for i, c in enumerate(cities)}
    locations = [
        Location(
            idx=i, id=r.location_id, name=r.name, city=r.city, state=r.state,
            lat=float(r.lat), lon=float(r.lon), type=r.location_type,
            dock_open=int(r.dock_open_hour), dock_close=int(r.dock_close_hour),
            dwell=int(r.avg_dwell_minutes), city_idx=city_idx[f"{r.city}, {r.state}"],
        )
        for i, r in enumerate(ldf.itertuples())
    ]
    loc_index = {l.id: l.idx for l in locations}
    n = len(locations)

    dm = pd.read_csv(data_dir / "distance_matrix.csv")
    fi = dm.from_location_id.map(loc_index).to_numpy()
    ti = dm.to_location_id.map(loc_index).to_numpy()
    dist = np.zeros((n, n))
    drv = np.zeros((n, n))
    dist[fi, ti] = dm.road_miles.to_numpy()
    drv[fi, ti] = dm.drive_minutes.to_numpy()

    tdf = pd.read_csv(data_dir / "trucks.csv")
    trucks = []
    for i, r in enumerate(tdf.itertuples()):
        avail = _minutes(r.available_at, start)
        # cost_per_mile_usd is treated as the all-in operating cost (fuel, driver pay,
        # equipment); tolls are added on top. Rates in loads.csv (~$2.3-2.8/mi) line up with this.
        cpm = float(r.cost_per_mile_usd) + params.toll_per_mile
        trucks.append(Truck(
            idx=i, id=r.truck_id, driver_id=r.driver_id,
            home=loc_index[r.domicile_location_id], start=loc_index[r.start_location_id],
            trailer=r.trailer_type, capacity_lbs=int(r.capacity_lbs), team=_bool(r.team_drivers),
            hos_drive=float(r.hos_drive_remaining_hr) * 60, hos_duty=float(r.hos_duty_remaining_hr) * 60,
            hos_cycle=float(r.hos_cycle_remaining_hr) * 60, avail=avail, max_days=int(r.max_tour_days),
            deadline=avail + int(r.max_tour_days) * 1440, cost_per_mile=cpm,
        ))

    ddf = pd.read_csv(data_dir / "loads.csv")
    loads = []
    for i, r in enumerate(ddf.itertuples()):
        o, d = loc_index[r.origin_location_id], loc_index[r.destination_location_id]
        loads.append(Load(
            idx=i, id=r.load_id, o=o, d=d, origin_city=r.origin_city, dest_city=r.destination_city,
            trailer=r.trailer_type, commodity=r.commodity, weight=int(r.weight_lbs), hazmat=_bool(r.hazmat),
            pe=_minutes(r.pickup_earliest, start), pl=_minutes(r.pickup_latest, start),
            de=_minutes(r.delivery_earliest, start), dl=_minutes(r.delivery_latest, start),
            miles=float(dist[o, d]), rate=float(r.rate_usd), priority=r.priority, status=r.status,
            cancel_p=float(r.cancel_probability),
        ))

    # metro-level neighbourhoods, used to find candidate trucks quickly
    clat = np.zeros(len(cities))
    clon = np.zeros(len(cities))
    cnt = np.zeros(len(cities))
    for l in locations:
        clat[l.city_idx] += l.lat
        clon[l.city_idx] += l.lon
        cnt[l.city_idx] += 1
    clat /= cnt
    clon /= cnt
    lat1, lat2 = np.radians(clat)[:, None], np.radians(clat)[None, :]
    dlon = np.radians(clon)[None, :] - np.radians(clon)[:, None]
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    cdist = 2 * 3958.8 * np.arcsin(np.sqrt(a)) * 1.2
    city_near = [list(np.where(cdist[c] <= 320)[0]) for c in range(len(cities))]

    return Problem(
        locations=locations, trucks=trucks, loads=loads, params=params,
        dist=dist.tolist(), drive=drv.tolist(), cities=cities, city_near=city_near, loc_index=loc_index,
    )
