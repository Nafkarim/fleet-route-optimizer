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
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from optimizer.data import load_problem  # noqa: E402
from optimizer.run import OUT_DIR, print_kpis, run_plan, save  # noqa: E402
from optimizer.solver import SolveSettings  # noqa: E402
from server.chat import MODEL, ChatService, PlanData, credentials_configured, load_dotenv  # noqa: E402

load_dotenv()

app = FastAPI(title="Fleet Route Optimizer")

_lock = threading.Lock()
_state = {"summary": None, "detail": None, "running": False, "phase": "Idle", "pct": 0.0,
          "objective": None, "iterations": 0, "error": None, "plan_version": 0}
_chat_cache: dict = {"version": -1, "data": None, "problem": None}


def _plan_data() -> tuple[PlanData, str]:
    """Chat view of the current plan; rebuilt whenever a new plan is loaded."""
    with _lock:
        summary, detail, version = _state["summary"], _state["detail"], _state["plan_version"]
    if summary is None:
        raise HTTPException(404, "No plan yet - the optimizer is still running.")
    if _chat_cache["version"] != version:
        if _chat_cache["problem"] is None:
            _chat_cache["problem"] = load_problem()
        _chat_cache.update(version=version, data=PlanData(summary, detail, _chat_cache["problem"]))
    return _chat_cache["data"], str(version)


chat_service = ChatService(_plan_data)


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
            _state.update(summary=summary, detail=detail, phase="Done", pct=100.0, error=None,
                          plan_version=_state["plan_version"] + 1)
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


@app.get("/api/load/{load_id}")
def load_lookup(load_id: str):
    data, _ = _plan_data()
    lid = load_id.strip().upper()
    if lid in data.load_truck:
        return {"load_id": lid, "truck_id": data.load_truck[lid][0]}
    if lid in data.uncovered:
        return {"load_id": lid, "uncovered": True}
    raise HTTPException(404, f"Unknown load {load_id}")


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    conversation_id: str | None = None


@app.get("/api/chat/status")
def chat_status():
    return {"configured": credentials_configured(), "model": MODEL}


@app.post("/api/chat")
def chat(req: ChatRequest):
    if not credentials_configured():
        raise HTTPException(503, "No Anthropic API key configured. Add ANTHROPIC_API_KEY to .env and restart.")
    _plan_data()  # 404 early if there is no plan yet

    def gen():
        for event in chat_service.stream(req.conversation_id, req.message.strip()):
            yield json.dumps(event) + "\n"
        yield json.dumps({"type": "done"}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


WEB = ROOT / "web"


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=WEB), name="static")
