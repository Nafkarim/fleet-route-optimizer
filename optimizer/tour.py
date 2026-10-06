"""Tour simulation: the single source of truth for feasibility and cost.

A tour is an ordered list of loads for one truck. The truck leaves its start
location, for each load drives empty to the origin (deadhead), loads, drives
loaded to the destination, unloads, and finally drives empty back to its
domicile. Along the way we enforce:

  * pickup / delivery time windows and dock opening hours (waiting if early)
  * hours of service: 11 h driving & 14 h on-duty window per shift, a 30 min
    break after 8 h of driving, a 10 h reset between shifts, a 70 h cycle
    (34 h restart when exhausted); team trucks get doubled shift limits
  * return to domicile no later than (available_at + max_tour_days)

Times are minutes since the horizon start.
"""
from __future__ import annotations

from dataclasses import dataclass

from .data import Load, Problem, Truck

INF = float("inf")
EPS = 1e-6

# state tuple layout
T, LOC, DRV, WIN, SINCE_BRK, CYC, MILES, EMPTY, LOADED, LATE_DEL, WAIT, FRESH = range(12)


@dataclass
class TourResult:
    cost: float
    states: list  # state after 0, 1, ..., n loads (before returning home)
    return_time: float
    late_return: float
    miles: float
    empty_miles: float
    loaded_miles: float
    late_delivery: float
    wait: float
    events: list | None = None  # (kind, start, end, extra dict)
    legs: list | None = None    # (from_loc, to_loc, loaded, miles, depart, arrive, load_id)


class Simulator:
    def __init__(self, prob: Problem, deadhead_penalty: float | None = None):
        self.p = prob
        self.D = prob.dist
        self.DT = prob.drive
        self.locs = prob.locations
        pr = prob.params
        self.pr = pr
        self.dhp = pr.deadhead_penalty if deadhead_penalty is None else deadhead_penalty

    # ------------------------------------------------------------------ helpers
    def limits(self, truck: Truck):
        pr = self.pr
        if truck.team:
            return pr.max_drive * 2, pr.max_duty * 2, INF, pr.cycle_limit * 2
        return pr.max_drive, pr.max_duty, pr.break_after, pr.cycle_limit

    def initial(self, truck: Truck):
        m = 2 if truck.team else 1
        return (truck.avail, truck.start, truck.hos_drive * m, truck.hos_duty * m, 0.0,
                truck.hos_cycle * m, 0.0, 0.0, 0.0, 0.0, 0.0, True)

    def dock_ready(self, t: float, loc: int) -> float:
        """Earliest time >= t at which the dock at loc is open."""
        L = self.locs[loc]
        if L.dock_open == 0 and L.dock_close >= 24:
            return t
        day, h = divmod(t, 1440.0)
        o, c = L.dock_open * 60, L.dock_close * 60
        if o <= h < c:
            return t
        if h < o:
            return day * 1440 + o
        return (day + 1) * 1440 + o

    def _drive(self, truck, st, minutes, kind, rec):
        """Drive for `minutes`, inserting breaks / resets / restarts as required."""
        t, dl, wl, sb, cl = st
        full_d, full_w, brk, full_c = self.limits(truck)
        pr = self.pr
        while minutes > EPS:
            lim = min(minutes, dl, wl, cl, brk - sb)
            if lim <= EPS:
                if cl <= EPS:
                    if rec is not None:
                        rec.append(("restart", t, t + pr.restart_len))
                    t += pr.restart_len
                    dl, wl, sb, cl = full_d, full_w, 0.0, full_c
                elif dl <= EPS or wl <= pr.break_len + EPS:
                    if rec is not None:
                        rec.append(("rest", t, t + pr.reset_len))
                    t += pr.reset_len
                    dl, wl, sb = full_d, full_w, 0.0
                else:
                    if rec is not None:
                        rec.append(("break", t, t + pr.break_len))
                    t += pr.break_len
                    wl -= pr.break_len
                    sb = 0.0
                continue
            if rec is not None:
                rec.append((kind, t, t + lim))
            t += lim
            minutes -= lim
            dl -= lim
            wl -= lim
            sb += lim
            cl -= lim
        return t, dl, wl, sb, cl

    def _service(self, truck, st, loc, open_t, hard_latest, kind, rec):
        """Arrive at loc, wait for window/dock, then load or unload.

        Returns (state5, service_start, waited_on_duty) or None if the hard latest is missed.
        """
        t, dl, wl, sb, cl = st
        full_d, full_w, _, full_c = self.limits(truck)
        pr = self.pr
        dwell = self.locs[loc].dwell
        if cl < dwell:
            if rec is not None:
                rec.append(("restart", t, t + pr.restart_len))
            t += pr.restart_len
            dl, wl, sb, cl = full_d, full_w, 0.0, full_c
        s = self.dock_ready(max(t, open_t), loc)
        if s > hard_latest + EPS:
            return None
        wait = s - t
        paid_wait = 0.0
        if wait >= pr.reset_len:
            # long wait: the driver is off duty long enough to reset the shift
            if rec is not None:
                rec.append(("wait", t, s))
            dl, wl, sb = full_d, full_w, 0.0
        elif wl - wait < dwell:
            # not enough on-duty window left: take a 10 h reset first
            tr = t + pr.reset_len
            if rec is not None:
                rec.append(("rest", t, tr))
            dl, wl, sb = full_d, full_w, 0.0
            s = self.dock_ready(max(tr, open_t), loc)
            if s > hard_latest + EPS:
                return None
            w2 = s - tr
            if w2 >= pr.reset_len:
                pass
            elif wl - w2 < dwell:
                return None
            else:
                wl -= w2
                paid_wait = w2
            if rec is not None and s > tr:
                rec.append(("wait", tr, s))
        else:
            wl -= wait
            paid_wait = wait
            if wait >= pr.break_len:
                sb = 0.0
            if rec is not None and wait > 0:
                rec.append(("wait", t, s))
        if rec is not None:
            rec.append((kind, s, s + dwell))
        t = s + dwell
        wl -= dwell
        cl -= dwell
        if dwell >= pr.break_len:
            sb = 0.0
        return (t, dl, wl, sb, cl), s, paid_wait

    # ------------------------------------------------------------------ core
    def add_load(self, truck: Truck, state, load: Load, rec=None, legs=None):
        """Extend a tour state by one load. Returns the new state or None if infeasible."""
        if load.trailer != truck.trailer or load.weight > truck.capacity_lbs:
            return None
        t, loc = state[T], state[LOC]
        st = (t, state[DRV], state[WIN], state[SINCE_BRK], state[CYC])
        D, DT = self.D, self.DT
        dh_mi = D[loc][load.o]
        dh_min = DT[loc][load.o]

        # A fresh truck can stay at home (off duty) and leave just in time.
        if state[FRESH]:
            target = self.dock_ready(load.pe, load.o)
            depart = max(t, target - dh_min)
            if depart - t >= self.pr.reset_len:
                full_d, full_w, _, _ = self.limits(truck)
                st = (depart, full_d, full_w, 0.0, st[4])
            else:
                st = (depart,) + st[1:]
        elif dh_mi > 0:
            # Don't leave so early that we just sit at the origin burning the duty window.
            target = self.dock_ready(load.pe, load.o)
            idle = target - dh_min - t
            if idle >= self.pr.reset_len:
                full_d, full_w, _, _ = self.limits(truck)
                if rec is not None:
                    rec.append(("wait", t, t + idle))
                st = (t + idle, full_d, full_w, 0.0, st[4])

        dep = st[0]
        if dh_mi > 0:
            st = self._drive(truck, st, dh_min, "drive_empty", rec)
            if legs is not None:
                legs.append((loc, load.o, False, dh_mi, dep, st[0], load.id))
        if st[0] > load.pl + EPS:
            return None
        r = self._service(truck, st, load.o, load.pe, load.pl, "load", rec)
        if r is None:
            return None
        st, _, w1 = r
        dep = st[0]
        st = self._drive(truck, st, DT[load.o][load.d], "drive_loaded", rec)
        if legs is not None:
            legs.append((load.o, load.d, True, load.miles, dep, st[0], load.id))
        if st[0] > load.dl + self.pr.late_delivery_allow + EPS:
            return None
        r = self._service(truck, st, load.d, load.de, load.dl + self.pr.late_delivery_allow, "unload", rec)
        if r is None:
            return None
        st, s2, w2 = r
        late = max(0.0, s2 - load.dl)
        if st[0] > truck.deadline + self.pr.late_return_allow:
            return None
        return (st[0], load.d, st[1], st[2], st[3], st[4],
                state[MILES] + dh_mi + load.miles, state[EMPTY] + dh_mi, state[LOADED] + load.miles,
                state[LATE_DEL] + late, state[WAIT] + w1 + w2, False)

    def finish(self, truck: Truck, state, ret_w: float = 1.0, rec=None, legs=None):
        """Drive home and price the tour. Returns (cost, return_time, late_return) or None."""
        if state[FRESH]:
            return 0.0, truck.avail, 0.0
        loc = state[LOC]
        pr = self.pr
        ret_mi = self.D[loc][truck.home]
        st = (state[T], state[DRV], state[WIN], state[SINCE_BRK], state[CYC])
        dep = st[0]
        if ret_mi > 0:
            st = self._drive(truck, st, self.DT[loc][truck.home], "drive_empty", rec)
            if legs is not None:
                legs.append((loc, truck.home, False, ret_mi, dep, st[0], None))
        ret_t = st[0]
        late_ret = max(0.0, ret_t - truck.deadline)
        if late_ret > pr.late_return_allow + EPS:
            return None
        cpm = truck.cost_per_mile
        cost = (state[MILES] * cpm + state[EMPTY] * self.dhp
                + ret_w * ret_mi * (cpm + self.dhp)
                + state[LATE_DEL] / 60 * pr.late_delivery_per_hour
                + state[WAIT] / 60 * pr.detention_per_hour
                + late_ret / 60 * pr.late_return_per_hour)
        return cost, ret_t, late_ret

    def simulate(self, truck: Truck, seq: list[Load], record: bool = False, ret_w: float = 1.0):
        """Simulate a full tour. Returns TourResult or None if infeasible."""
        rec = [] if record else None
        legs = [] if record else None
        state = self.initial(truck)
        states = [state]
        for ld in seq:
            state = self.add_load(truck, state, ld, rec, legs)
            if state is None:
                return None
            states.append(state)
        fin = self.finish(truck, state, ret_w, rec, legs)
        if fin is None:
            return None
        cost, ret_t, late_ret = fin
        ret_mi = 0.0 if state[FRESH] else self.D[state[LOC]][truck.home]
        return TourResult(
            cost=cost, states=states, return_time=ret_t, late_return=late_ret,
            miles=state[MILES] + ret_mi, empty_miles=state[EMPTY] + ret_mi, loaded_miles=state[LOADED],
            late_delivery=state[LATE_DEL], wait=state[WAIT], events=rec, legs=legs,
        )

    def extend_cost(self, truck: Truck, states: list, seq: list[Load], pos: int, new: Load, ret_w: float = 1.0):
        """Cost of the tour with `new` inserted at position `pos`, reusing the cached prefix state."""
        state = self.add_load(truck, states[pos], new)
        if state is None:
            return None
        for ld in seq[pos:]:
            state = self.add_load(truck, state, ld)
            if state is None:
                return None
        fin = self.finish(truck, state, ret_w)
        return None if fin is None else fin[0]

    def cost_of(self, truck: Truck, seq: list[Load], ret_w: float = 1.0):
        state = self.initial(truck)
        states = [state]
        for ld in seq:
            state = self.add_load(truck, state, ld)
            if state is None:
                return None, None
            states.append(state)
        fin = self.finish(truck, state, ret_w)
        if fin is None:
            return None, None
        return fin[0], states
