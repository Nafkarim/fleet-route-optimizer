"""Route optimizer: parallel cheapest insertion + Large Neighborhood Search.

Objective (minimise):
    sum over trucks of tour cost   (operating $ + deadhead penalty + late/wait penalties)
  + sum over unserved loads of     unserved_penalty x priority weight x (1 - cancel probability)

Every tour is checked by Simulator, so whatever the search does, the plan it
returns satisfies every hard constraint (closed tours, windows, HOS, trailer).
"""
from __future__ import annotations

import bisect
import math
import random
import time
from dataclasses import dataclass, field
from typing import Callable

from .data import PRIORITY_WEIGHT, Load, Problem
from .tour import LOC, T, Simulator

CONSTRUCT_RETURN_WEIGHT = 0.35  # optimism during construction: a backhaul home is likely to be found later
MAX_CANDIDATES = 14


@dataclass
class SolveSettings:
    deadhead_penalty: float | None = None
    unserved_penalty: float | None = None
    include_tentative: bool = True
    max_cancel_prob: float = 1.0
    time_limit: float = 45.0
    late_delivery_allow_h: float = 0.0  # >0 lets deliveries run late (penalised per hour)
    late_return_allow_h: float = 0.0    # >0 lets trucks get home late (penalised per hour)
    seed: int = 7


@dataclass
class Solution:
    seqs: list[list[Load]]
    unserved: set[int]
    excluded: set[int]
    objective: float
    history: list = field(default_factory=list)  # (seconds, objective) during search
    construct_objective: float = 0.0
    construct_seconds: float = 0.0
    seconds: float = 0.0
    iterations: int = 0


class Optimizer:
    def __init__(self, prob: Problem, settings: SolveSettings | None = None,
                 progress: Callable[[dict], None] | None = None):
        self.p = prob
        self.s = settings or SolveSettings()
        prob.params.late_delivery_allow = self.s.late_delivery_allow_h * 60
        prob.params.late_return_allow = self.s.late_return_allow_h * 60
        self.sim = Simulator(prob, self.s.deadhead_penalty)
        self.progress = progress or (lambda info: None)
        self.rng = random.Random(self.s.seed)
        upen = prob.params.unserved_penalty if self.s.unserved_penalty is None else self.s.unserved_penalty
        # leaving a load uncovered loses its revenue plus a service penalty (scaled by priority and
        # by how likely the load is to actually materialise)
        self.penalty = [(upen * PRIORITY_WEIGHT[l.priority] + l.rate) * (1 - l.cancel_p) for l in prob.loads]
        self.loc_city = [l.city_idx for l in prob.locations]
        self.active = [l for l in prob.loads
                       if (self.s.include_tentative or l.status != "tentative") and l.cancel_p <= self.s.max_cancel_prob]
        active_ids = {l.idx for l in self.active}
        self.excluded = {l.idx for l in prob.loads if l.idx not in active_ids}

        n_t = len(prob.trucks)
        self.seqs: list[list[Load]] = [[] for _ in range(n_t)]
        self.states: list[list] = [[self.sim.initial(t)] for t in prob.trucks]
        self.cost = [0.0] * n_t
        self.where: dict[int, int] = {}  # load idx -> truck idx
        self.ret_w = 1.0
        # metro index: trailer -> city -> trucks that live there or deliver there
        self.city_trucks: dict[str, list[set[int]]] = {
            tt: [set() for _ in prob.cities] for tt in {t.trailer for t in prob.trucks}}
        self.truck_cities: list[set[int]] = [set() for _ in range(n_t)]
        for t in prob.trucks:
            self._reindex(t.idx)

    # ------------------------------------------------------------------ bookkeeping
    def _reindex(self, k: int):
        t = self.p.trucks[k]
        new = {self.loc_city[t.home]} | {self.loc_city[l.d] for l in self.seqs[k]}
        old = self.truck_cities[k]
        idx = self.city_trucks[t.trailer]
        for c in old - new:
            idx[c].discard(k)
        for c in new - old:
            idx[c].add(k)
        self.truck_cities[k] = new

    def _set_seq(self, k: int, seq: list[Load]) -> bool:
        cost, states = self.sim.cost_of(self.p.trucks[k], seq, self.ret_w)
        if cost is None:
            return False
        for l in self.seqs[k]:
            self.where.pop(l.idx, None)
        self.seqs[k] = seq
        self.states[k] = states
        self.cost[k] = cost
        for l in seq:
            self.where[l.idx] = k
        self._reindex(k)
        return True

    def objective(self, unserved) -> float:
        return sum(self.cost) + sum(self.penalty[i] for i in unserved)

    # ------------------------------------------------------------------ insertion
    def candidates(self, load: Load, banned: set[int] | None = None):
        """Cheap pre-filter: trucks that could plausibly be near the origin in time."""
        city = self.loc_city[load.o]
        idx = self.city_trucks.get(load.trailer)
        if idx is None:
            return []
        pool = set()
        for c in self.p.city_near[city]:
            pool |= idx[c]
        D, DT = self.sim.D, self.sim.DT
        trucks = self.p.trucks
        out = []
        for k in pool:
            if banned and k in banned:
                continue
            tr = trucks[k]
            if load.pe > tr.deadline or load.weight > tr.capacity_lbs:
                continue
            seq = self.seqs[k]
            pos = bisect.bisect_left([l.pe for l in seq], load.pe)
            prev = self.states[k][pos]
            if prev[T] + DT[prev[LOC]][load.o] > load.pl:
                continue
            if pos < len(seq) and load.pe + DT[load.o][load.d] + DT[load.d][seq[pos].o] > seq[pos].pl:
                continue
            out.append((D[prev[LOC]][load.o], k, pos))
        out.sort()
        return out[:MAX_CANDIDATES]

    def best_insertion(self, load: Load, banned: set[int] | None = None, noise: float = 0.0):
        best = (math.inf, -1, -1)
        trucks = self.p.trucks
        for _, k, pos in self.candidates(load, banned):
            c = self.sim.extend_cost(trucks[k], self.states[k], self.seqs[k], pos, load, self.ret_w)
            if c is None:
                continue
            delta = c - self.cost[k]
            if noise:
                delta *= 1 + noise * (self.rng.random() - 0.5)
            if delta < best[0]:
                best = (delta, k, pos)
        return best

    def insert(self, load: Load, k: int, pos: int) -> bool:
        seq = self.seqs[k][:pos] + [load] + self.seqs[k][pos:]
        return self._set_seq(k, seq)

    # ------------------------------------------------------------------ construction
    def construct(self) -> set[int]:
        self.ret_w = CONSTRUCT_RETURN_WEIGHT
        unserved = set()
        loads = sorted(self.active, key=lambda l: (l.pe, l.pl))
        n = len(loads)
        for i, load in enumerate(loads):
            delta, k, pos = self.best_insertion(load)
            if k >= 0 and delta < self.penalty[load.idx] and self.insert(load, k, pos):
                pass
            else:
                unserved.add(load.idx)
            if i % 250 == 0:
                self.progress({"phase": "Building first plan", "pct": 5 + 25 * i / n})
        # re-price everything with the true cost of driving home empty
        self.ret_w = 1.0
        for k in range(len(self.seqs)):
            if self.seqs[k]:
                ok = self._set_seq(k, self.seqs[k])
                assert ok
        return unserved

    # ------------------------------------------------------------------ LNS
    def _destroy(self, q: int):
        assigned = list(self.where.keys())
        if not assigned:
            return set()
        op = self.rng.random()
        loads = self.p.loads
        if op < 0.35:  # related: same region & time
            seed = loads[self.rng.choice(assigned)]
            near = set(self.p.city_near[self.loc_city[seed.o]])
            rel = [i for i in assigned
                   if self.loc_city[loads[i].o] in near and abs(loads[i].pe - seed.pe) < 36 * 60]
            self.rng.shuffle(rel)
            return set(rel[:q]) | {seed.idx}
        if op < 0.6:  # worst tours: high empty share
            used = [k for k in range(len(self.seqs)) if self.seqs[k]]
            picks = set()
            for _ in range(3):
                sample = self.rng.sample(used, min(12, len(used)))
                k = max(sample, key=lambda k: self.cost[k] / (1 + sum(l.miles for l in self.seqs[k])))
                picks.add(k)
            return {l.idx for k in picks for l in self.seqs[k]}
        if op < 0.8:  # all trucks from one home city
            k0 = self.where[self.rng.choice(assigned)]
            home_city = self.loc_city[self.p.trucks[k0].home]
            ks = [k for k in range(len(self.seqs)) if self.seqs[k]
                  and self.loc_city[self.p.trucks[k].home] == home_city]
            self.rng.shuffle(ks)
            out = set()
            for k in ks:
                out |= {l.idx for l in self.seqs[k]}
                if len(out) >= q:
                    break
            return out
        return set(self.rng.sample(assigned, min(q, len(assigned))))  # random

    def lns(self, unserved: set[int], time_limit: float, t0: float):
        loads = self.p.loads
        cur = self.objective(unserved)
        best = cur
        best_seqs = [list(s) for s in self.seqs]
        best_unserved = set(unserved)
        dirty: set[int] = set()  # trucks changed since the best solution was recorded
        history = [(time.time() - t0, cur)]
        temp0 = 400.0
        start = time.time()
        it = 0
        unserved_by_city: dict[int, set[int]] = {}
        for i in unserved:
            unserved_by_city.setdefault(self.loc_city[loads[i].o], set()).add(i)

        while time.time() - start < time_limit:
            it += 1
            frac = (time.time() - start) / time_limit
            temp = temp0 * (1 - frac) + 1
            q = self.rng.randint(8, 30)
            removed = self._destroy(q)
            if not removed:
                break
            backup: dict[int, tuple] = {}

            def touch(k):
                if k not in backup:
                    backup[k] = (self.seqs[k], self.states[k], self.cost[k])

            # remove
            by_truck: dict[int, set[int]] = {}
            for i in removed:
                by_truck.setdefault(self.where[i], set()).add(i)
            for k, ids in by_truck.items():
                touch(k)
                keep = [l for l in self.seqs[k] if l.idx not in ids]
                if not self._set_seq(k, keep):
                    removed |= {l.idx for l in keep}
                    self._set_seq(k, [])
            # pool: removed loads + related unserved loads
            cities = set()
            for i in removed:
                cities.add(self.loc_city[loads[i].o])
                cities.add(self.loc_city[loads[i].d])
            for k in by_truck:
                cities.add(self.loc_city[self.p.trucks[k].home])
            extra = []
            for c in cities:
                extra.extend(unserved_by_city.get(c, ()))
            self.rng.shuffle(extra)
            pool = list(removed) + extra[:25]
            new_unserved = set(unserved) | removed
            jitter = 6 * 60
            pool.sort(key=lambda i: loads[i].pe + self.rng.uniform(-jitter, jitter))
            for i in pool:
                load = loads[i]
                delta, k, pos = self.best_insertion(load, noise=0.1)
                if k >= 0 and delta < self.penalty[i]:
                    touch(k)
                    if self.insert(load, k, pos):
                        new_unserved.discard(i)
            new_obj = (cur + sum(self.cost[k] - backup[k][2] for k in backup)
                       + sum(self.penalty[i] for i in new_unserved) - sum(self.penalty[i] for i in unserved))
            if new_obj < cur - 1e-6 or self.rng.random() < math.exp(-(new_obj - cur) / temp):
                for i in unserved - new_unserved:
                    unserved_by_city[self.loc_city[loads[i].o]].discard(i)
                for i in new_unserved - unserved:
                    unserved_by_city.setdefault(self.loc_city[loads[i].o], set()).add(i)
                unserved = new_unserved
                cur = new_obj
                dirty |= backup.keys()
                if cur < best - 1e-6:
                    best = cur
                    best_unserved = set(unserved)
                    for k in dirty:
                        best_seqs[k] = list(self.seqs[k])
                    dirty.clear()
            else:  # roll back
                for k in backup:
                    for l in self.seqs[k]:
                        self.where.pop(l.idx, None)
                for k, (seq, states, cost) in backup.items():
                    self.seqs[k], self.states[k], self.cost[k] = seq, states, cost
                    for l in seq:
                        self.where[l.idx] = k
                    self._reindex(k)
            if it % 25 == 0:
                history.append((time.time() - t0, best))
                self.progress({"phase": "Improving routes", "pct": 30 + 68 * frac,
                               "objective": best, "iterations": it})
        # restore best
        for k in range(len(self.seqs)):
            if [l.idx for l in self.seqs[k]] != [l.idx for l in best_seqs[k]]:
                ok = self._set_seq(k, best_seqs[k])
                assert ok
        self.where = {l.idx: k for k, seq in enumerate(self.seqs) for l in seq}
        history.append((time.time() - t0, best))
        return best_unserved, history, it

    # ------------------------------------------------------------------ main
    def solve(self) -> Solution:
        t0 = time.time()
        self.progress({"phase": "Building first plan", "pct": 5})
        unserved = self.construct()
        construct_obj = self.objective(unserved)
        construct_s = time.time() - t0
        self.progress({"phase": "Improving routes", "pct": 30, "objective": construct_obj})
        unserved, history, iters = self.lns(unserved, self.s.time_limit, t0)
        history.insert(0, (construct_s, construct_obj))
        obj = self.objective(unserved)
        return Solution(seqs=[list(s) for s in self.seqs], unserved=unserved, excluded=self.excluded,
                        objective=obj, history=history, construct_objective=construct_obj,
                        construct_seconds=construct_s, seconds=time.time() - t0, iterations=iters)
