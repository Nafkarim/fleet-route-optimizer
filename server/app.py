"""Local web app: serves the dashboard and runs the optimizer on demand.

Run:  .venv/bin/uvicorn server.app:app --port 8000
"""
from __future__ import annotations

import json
import sys
import threading
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from optimizer.run import OUT_DIR, print_kpis, run_plan, save  # noqa: E402
from optimizer.solver import SolveSettings  # noqa: E402

app = FastAPI(title="Fleet Route Optimizer")

_lock = threading.Lock()
_state = {"summary": None, "detail": None, "running": False, "phase": "Idle", "pct": 0.0,
          "objective": None, "iterations": 0, "error": None}


class SolveRequest(BaseModel):
    deadhead_penalty: float = Field(3.5, ge=0, le=20)
    unserved_penalty: float = Field(5000, ge=0, le=50000)
    include_tentative: bool = True
    max_cancel_prob: float = Field(1.0, ge=0, le=1)
    late_delivery_allow_h: float = Field(0, ge=0, le=12)
    late_return_allow_h: float = Field(0, ge=0, le=24)
    time_limit: float = Field(45, ge=5, le=600)


def _progress(info: dict):
    with _lock:
        _state.update({k: v for k, v in info.items() if k in ("phase", "pct", "objective", "iterations")})


def _solve(req: SolveRequest):
    try:
        summary, detail = run_plan(SolveSettings(**req.model_dump()), _progress)
        save(summary, detail)
        print_kpis(summary)
        with _lock:
            _state.update(summary=summary, detail=detail, phase="Done", pct=100.0, error=None)
    except Exception as e:  # surface solver errors in the UI instead of dying silently
        traceback.print_exc()
        with _lock:
            _state.update(error=str(e), phase="Failed")
    finally:
        with _lock:
            _state["running"] = False


def _start(req: SolveRequest) -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, phase="Starting", pct=0.0, objective=None, iterations=0, error=None)
    threading.Thread(target=_solve, args=(req,), daemon=True).start()
    return True


@app.on_event("startup")
def _load_or_solve():
    plan, det = OUT_DIR / "plan.json", OUT_DIR / "details.json"
    if plan.exists() and det.exists():
        _state["summary"] = json.loads(plan.read_text())
        _state["detail"] = json.loads(det.read_text())
        _state["phase"] = "Done"
        _state["pct"] = 100.0
    else:
        _start(SolveRequest())


@app.get("/api/status")
def status():
    with _lock:
        return {k: _state[k] for k in ("running", "phase", "pct", "objective", "iterations", "error")} | {
            "has_plan": _state["summary"] is not None}


@app.get("/api/plan")
def plan():
    if _state["summary"] is None:
        raise HTTPException(404, "No plan yet - the optimizer is still running.")
    return _state["summary"]


@app.get("/api/truck/{truck_id}")
def truck(truck_id: str):
    det = _state["detail"] or {}
    if truck_id not in det:
        raise HTTPException(404, f"Unknown truck {truck_id}")
    return det[truck_id]


@app.post("/api/solve")
def solve(req: SolveRequest):
    if not _start(req):
        raise HTTPException(409, "The optimizer is already running.")
    return {"started": True}


WEB = ROOT / "web"


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=WEB), name="static")
