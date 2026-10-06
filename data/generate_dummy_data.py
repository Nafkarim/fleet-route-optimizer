"""
Dummy data generator for the fleet routing project.

Produces:
  locations.csv        500 locations clustered around real US metro areas
  trucks.csv           1,000 trucks domiciled at terminal locations
  loads.csv            ~5,000 loads over a 7-day planning horizon
  distance_matrix.csv  road-distance and drive-time estimates for every location pair
  cost_params.json     cost and constraint parameters for the optimizer

Run:  python generate_dummy_data.py   (seeded, so output is reproducible)
"""
import csv
import json
import math
import random
from datetime import datetime, timedelta

SEED = 42
N_LOCATIONS = 500
N_TERMINALS = 40
N_TRUCKS = 1000
N_LOADS = 5000
HORIZON_START = datetime(2026, 10, 12, 0, 0)  # Monday
HORIZON_DAYS = 7
CIRCUITY = 1.2          # road miles / straight-line miles
AVG_SPEED_MPH = 55

random.seed(SEED)

# (city, state, lat, lon, weight) - weight drives how many locations land there
METROS = [
    ("Houston", "TX", 29.76, -95.37, 10), ("Dallas", "TX", 32.78, -96.80, 10),
    ("San Antonio", "TX", 29.42, -98.49, 5), ("Austin", "TX", 30.27, -97.74, 4),
    ("Atlanta", "GA", 33.75, -84.39, 9), ("Chicago", "IL", 41.88, -87.63, 10),
    ("Los Angeles", "CA", 34.05, -118.24, 10), ("Phoenix", "AZ", 33.45, -112.07, 5),
    ("Memphis", "TN", 35.15, -90.05, 6), ("Nashville", "TN", 36.16, -86.78, 5),
    ("Indianapolis", "IN", 39.77, -86.16, 6), ("Columbus", "OH", 39.96, -83.00, 5),
    ("Kansas City", "MO", 39.10, -94.58, 5), ("St. Louis", "MO", 38.63, -90.20, 5),
    ("Denver", "CO", 39.74, -104.99, 4), ("Charlotte", "NC", 35.23, -80.84, 5),
    ("Jacksonville", "FL", 30.33, -81.66, 4), ("Miami", "FL", 25.76, -80.19, 4),
    ("Orlando", "FL", 28.54, -81.38, 4), ("New Orleans", "LA", 29.95, -90.07, 3),
    ("Oklahoma City", "OK", 35.47, -97.52, 3), ("Little Rock", "AR", 34.75, -92.29, 3),
    ("Birmingham", "AL", 33.52, -86.80, 3), ("Louisville", "KY", 38.25, -85.76, 4),
    ("Detroit", "MI", 42.33, -83.05, 4), ("Minneapolis", "MN", 44.98, -93.27, 4),
    ("Harrisburg", "PA", 40.27, -76.88, 4), ("Newark", "NJ", 40.74, -74.17, 5),
    ("El Paso", "TX", 31.76, -106.49, 2), ("Laredo", "TX", 27.53, -99.48, 3),
]

TRAILER_TYPES = [("dry_van", 0.70), ("reefer", 0.20), ("flatbed", 0.10)]
TRAILER_SPECS = {
    "dry_van": {"capacity_lbs": 45000, "capacity_cuft": 3800, "length_ft": 53},
    "reefer":  {"capacity_lbs": 43000, "capacity_cuft": 3500, "length_ft": 53},
    "flatbed": {"capacity_lbs": 48000, "capacity_cuft": 0,    "length_ft": 48},
}
COMMODITIES = {
    "dry_van": ["consumer goods", "paper products", "electronics", "beverages", "auto parts", "retail"],
    "reefer":  ["produce", "frozen food", "dairy", "meat", "pharmaceuticals"],
    "flatbed": ["steel", "lumber", "machinery", "building materials", "pipe"],
}


def weighted_choice(pairs):
    items, weights = zip(*pairs)
    return random.choices(items, weights=weights, k=1)[0]


def haversine_mi(lat1, lon1, lat2, lon2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def road_miles(a, b):
    if a["location_id"] == b["location_id"]:
        return 0.0
    return round(max(haversine_mi(a["lat"], a["lon"], b["lat"], b["lon"]) * CIRCUITY, 3.0), 1)


def fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


# ---------------------------------------------------------------- locations
metro_weights = [(m, m[4]) for m in METROS]
locations = []
# guarantee every metro gets at least one terminal
terminal_metros = [m for m in METROS] + [weighted_choice(metro_weights) for _ in range(N_TERMINALS - len(METROS))]

for i in range(N_LOCATIONS):
    is_terminal = i < N_TERMINALS
    metro = terminal_metros[i] if is_terminal else weighted_choice(metro_weights)
    city, state, mlat, mlon, _ = metro
    lat = round(mlat + random.gauss(0, 0.18), 5)
    lon = round(mlon + random.gauss(0, 0.22), 5)
    if is_terminal:
        loc_type, name = "terminal", f"{city} Terminal {i + 1}"
        open_h, close_h, dwell = 0, 24, 30
    else:
        loc_type = random.choices(["shipper", "consignee", "both"], weights=[0.35, 0.35, 0.30])[0]
        kind = random.choice(["DC", "Plant", "Warehouse", "Cross-Dock", "Mill", "Store Hub"])
        name = f"{city} {kind} {i + 1}"
        open_h = random.choice([0, 5, 6, 7, 8])
        close_h = 24 if open_h == 0 else random.choice([16, 17, 18, 20, 22])
        dwell = int(random.triangular(30, 240, 90))
    locations.append({
        "location_id": f"L{i + 1:03d}",
        "name": name,
        "city": city,
        "state": state,
        "lat": lat,
        "lon": lon,
        "location_type": loc_type,
        "dock_open_hour": open_h,
        "dock_close_hour": close_h,
        "avg_dwell_minutes": dwell,
        "appointment_required": loc_type != "terminal" and random.random() < 0.6,
    })

terminals = [l for l in locations if l["location_type"] == "terminal"]
origins = [l for l in locations if l["location_type"] in ("shipper", "both")]
dests = [l for l in locations if l["location_type"] in ("consignee", "both")]

# Lane imbalance: freight flows out of big production/port metros more than into them
OUTBOUND_BIAS = {"Los Angeles": 1.8, "Houston": 1.5, "Laredo": 1.6, "Chicago": 1.3,
                 "Newark": 1.4, "Memphis": 1.2, "Miami": 0.6, "Denver": 0.6,
                 "Phoenix": 0.7, "Orlando": 0.6, "El Paso": 1.1}
origin_weights = [(l, OUTBOUND_BIAS.get(l["city"], 1.0)) for l in origins]
dest_weights = [(l, 1.0 / OUTBOUND_BIAS.get(l["city"], 1.0)) for l in dests]

# ---------------------------------------------------------------- trucks
term_weights = [(t, next(m[4] for m in METROS if m[0] == t["city"])) for t in terminals]
trucks = []
for i in range(N_TRUCKS):
    dom = weighted_choice(term_weights)
    ttype = weighted_choice(TRAILER_TYPES)
    spec = TRAILER_SPECS[ttype]
    team = random.random() < 0.08
    avail = HORIZON_START + timedelta(hours=random.choice([0, 0, 0, 4, 8, 12, 24]))
    trucks.append({
        "truck_id": f"T{i + 1:04d}",
        "driver_id": f"D{i + 1:04d}",
        "domicile_location_id": dom["location_id"],
        "start_location_id": dom["location_id"],
        "trailer_type": ttype,
        "capacity_lbs": spec["capacity_lbs"],
        "capacity_cuft": spec["capacity_cuft"],
        "team_drivers": team,
        "hos_drive_remaining_hr": round(random.uniform(6, 11), 1),
        "hos_duty_remaining_hr": round(random.uniform(9, 14), 1),
        "hos_cycle_remaining_hr": round(random.uniform(30, 70), 1),
        "available_at": fmt(avail),
        "max_tour_days": random.choice([3, 5, 5, 5, 7]),
        "cost_per_mile_usd": round(random.uniform(1.65, 2.10), 2),
        "mpg": round(random.uniform(6.2, 7.8), 1),
    })

# ---------------------------------------------------------------- loads
loads = []
attempts = 0
while len(loads) < N_LOADS and attempts < N_LOADS * 20:
    attempts += 1
    o = weighted_choice(origin_weights)
    d = weighted_choice(dest_weights)
    if o["location_id"] == d["location_id"]:
        continue
    miles = road_miles(o, d)
    # keep mostly regional/OTR lengths, allow some long haul
    if miles < 50 or miles > 2200 or (miles > 1200 and random.random() < 0.6):
        continue
    ttype = weighted_choice(TRAILER_TYPES)
    spec = TRAILER_SPECS[ttype]
    drive_hr = miles / AVG_SPEED_MPH
    # add mandatory 10-hr breaks for every 11 hrs of driving
    transit_hr = drive_hr + 10 * int(drive_hr // 11) + o["avg_dwell_minutes"] / 60

    day = random.randrange(HORIZON_DAYS - 1)
    open_h = o["dock_open_hour"]
    close_h = o["dock_close_hour"]
    start_h = random.uniform(open_h, max(open_h + 1, close_h - 3))
    pu_early = HORIZON_START + timedelta(days=day, hours=start_h)
    pu_early = pu_early.replace(minute=(pu_early.minute // 15) * 15, second=0, microsecond=0)
    pu_late = pu_early + timedelta(hours=random.choice([2, 2, 4, 6, 8]))
    del_early = pu_early + timedelta(hours=math.ceil(transit_hr))
    del_late = del_early + timedelta(hours=random.choice([4, 6, 8, 12, 24]))

    # rate per mile: shorter hauls and reefers pay more; outbound-heavy lanes pay less on backhaul
    base_rpm = 2.10 + 250 / (miles + 100)
    base_rpm *= {"dry_van": 1.0, "reefer": 1.18, "flatbed": 1.12}[ttype]
    base_rpm *= 1 + 0.15 * (OUTBOUND_BIAS.get(o["city"], 1.0) < 1)  # tight outbound markets pay more
    rate = round(miles * base_rpm * random.uniform(0.88, 1.12) + 150, 2)

    weight = int(random.triangular(8000, spec["capacity_lbs"], 38000))
    loads.append({
        "load_id": f"LD{len(loads) + 1:05d}",
        "origin_location_id": o["location_id"],
        "destination_location_id": d["location_id"],
        "origin_city": f'{o["city"]}, {o["state"]}',
        "destination_city": f'{d["city"]}, {d["state"]}',
        "trailer_type": ttype,
        "commodity": random.choice(COMMODITIES[ttype]),
        "weight_lbs": weight,
        "hazmat": random.random() < 0.04,
        "pickup_earliest": fmt(pu_early),
        "pickup_latest": fmt(pu_late),
        "delivery_earliest": fmt(del_early),
        "delivery_latest": fmt(del_late),
        "loaded_miles": miles,
        "rate_usd": rate,
        "priority": random.choices(["standard", "high", "critical"], weights=[0.80, 0.15, 0.05])[0],
        "status": random.choices(["tendered", "accepted", "tentative"], weights=[0.55, 0.35, 0.10])[0],
        "cancel_probability": round(random.betavariate(1.2, 18), 3),
    })

loads.sort(key=lambda r: r["pickup_earliest"])

# ---------------------------------------------------------------- distance matrix
dist_rows = []
for a in locations:
    for b in locations:
        if a is b:
            continue
        m = road_miles(a, b)
        dist_rows.append({
            "from_location_id": a["location_id"],
            "to_location_id": b["location_id"],
            "road_miles": m,
            "drive_minutes": int(round(m / AVG_SPEED_MPH * 60)),
        })

# ---------------------------------------------------------------- cost params
cost_params = {
    "horizon_start": fmt(HORIZON_START),
    "horizon_days": HORIZON_DAYS,
    "avg_speed_mph": AVG_SPEED_MPH,
    "road_circuity_factor": CIRCUITY,
    "fuel_price_per_gallon_usd": 3.85,
    "driver_pay_per_mile_usd": 0.62,
    "driver_pay_per_hour_detention_usd": 25.0,
    "deadhead_penalty_per_mile_usd": 3.50,
    "unserved_load_penalty_usd": 5000,
    "late_delivery_penalty_per_hour_usd": 150,
    "late_return_to_domicile_penalty_per_hour_usd": 75,
    "toll_estimate_per_mile_usd": 0.06,
    "hos_rules": {
        "max_drive_hours_per_shift": 11,
        "max_duty_hours_per_shift": 14,
        "required_break_after_drive_hours": 8,
        "break_minutes": 30,
        "off_duty_reset_hours": 10,
        "cycle_limit_hours": 70,
        "cycle_days": 8,
    },
    "notes": "Deadhead penalty is intentionally high to discourage empty miles; tune it during backtesting.",
}

# ---------------------------------------------------------------- write
def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

write_csv("locations.csv", locations)
write_csv("trucks.csv", trucks)
write_csv("loads.csv", loads)
write_csv("distance_matrix.csv", dist_rows)
with open("cost_params.json", "w") as f:
    json.dump(cost_params, f, indent=2)

print(f"locations: {len(locations)}  (terminals: {len(terminals)}, origins: {len(origins)}, dests: {len(dests)})")
print(f"trucks:    {len(trucks)}")
print(f"loads:     {len(loads)}")
print(f"distance pairs: {len(dist_rows)}")
