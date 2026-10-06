"""Naive dispatcher used as a comparison point.

Mimics a busy dispatch desk: loads are handled in pickup order and each one goes
to the closest truck that can legally make it and still get home on time. No
look-ahead, no thought about where the truck ends up, no re-planning.
"""
from __future__ import annotations

from .data import Problem
from .solver import Optimizer, Solution, SolveSettings


def naive_dispatch(prob: Problem, settings: SolveSettings | None = None) -> Solution:
    opt = Optimizer(prob, settings)
    unserved = set()
    for load in sorted(opt.active, key=lambda l: (l.pe, l.pl)):
        placed = False
        for _, k, pos in opt.candidates(load):
            if pos != len(opt.seqs[k]):
                continue  # dispatchers only add to the end of a truck's schedule
            if opt.sim.extend_cost(prob.trucks[k], opt.states[k], opt.seqs[k], pos, load) is not None:
                placed = opt.insert(load, k, pos)
                if placed:
                    break
        if not placed:
            unserved.add(load.idx)
    obj = opt.objective(unserved)
    return Solution(seqs=[list(s) for s in opt.seqs], unserved=unserved, excluded=opt.excluded, objective=obj)
