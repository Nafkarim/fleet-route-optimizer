"""Chat assistant plumbing, tested with a scripted fake Claude client (no API calls)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from optimizer.data import load_problem  # noqa: E402
from server.chat import TOOLS, ChatService, PlanData, _validate  # noqa: E402


def text_block(t):
    return SimpleNamespace(type="text", text=t)


def tool_block(id_, name, inp):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=inp)


class FakeStream:
    def __init__(self, content, stop_reason):
        self.msg = SimpleNamespace(content=content, stop_reason=stop_reason)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        for b in self.msg.content:
            if b.type == "text":
                yield SimpleNamespace(type="text", text=b.text)

    def get_final_message(self):
        return self.msg


class FakeClient:
    """Replays a script of (content, stop_reason) turns and records each request."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})
        content, stop = self.script.pop(0)
        return FakeStream(content, stop)


@pytest.fixture(scope="module")
def data():
    out = ROOT / "output"
    if not (out / "plan.json").exists():
        pytest.skip("no saved plan - run python -m optimizer.run first")
    return PlanData(json.loads((out / "plan.json").read_text()), json.loads((out / "details.json").read_text()),
                    load_problem())


def make_service(data, script):
    svc = ChatService(lambda: (data, "v1"))
    svc._client = FakeClient(script)
    return svc


def test_tool_round_trip(data):
    svc = make_service(data, [
        ([text_block("Let me check. "), tool_block("tu1", "find_trucks", {"home_city": "Houston", "limit": 2})],
         "tool_use"),
        ([text_block("Houston has the busiest trucks: T0001.")], "end_turn"),
    ])
    events = list(svc.stream(None, "Which Houston trucks are busiest?"))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "conversation"
    assert "tool" in kinds
    assert "".join(e["text"] for e in events if e["type"] == "text") == "Let me check. Houston has the busiest trucks: T0001."

    conv = svc.convs[events[0]["id"]]
    roles = [m["role"] for m in conv.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    result = conv.messages[2]["content"][0]
    assert result["tool_use_id"] == "tu1" and "is_error" not in result
    assert json.loads(result["content"])["matching_trucks"] > 0

    req = svc.client.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["tools"] == TOOLS
    assert "Current plan snapshot" in req["system"]


def test_bad_tool_input_reported_as_error(data):
    svc = make_service(data, [
        ([tool_block("tu1", "truck_details", {"truck_id": "T9999"}),
          tool_block("tu2", "find_trucks", {"limit": "lots"})], "tool_use"),
        ([text_block("That truck doesn't exist.")], "end_turn"),
    ])
    events = list(svc.stream(None, "Show T9999"))
    conv = svc.convs[events[0]["id"]]
    results = conv.messages[2]["content"]
    assert len(results) == 2, "all tool results go back in a single user message"
    assert all(r["is_error"] for r in results)


def test_history_is_append_only_across_turns(data):
    svc = make_service(data, [
        ([text_block("First answer.")], "end_turn"),
        ([text_block("Second answer.")], "end_turn"),
    ])
    first = list(svc.stream(None, "Q1"))
    cid = first[0]["id"]
    before = list(svc.convs[cid].messages)
    list(svc.stream(cid, "Q2"))
    after = svc.convs[cid].messages
    assert after[:len(before)] == before
    assert svc.client.requests[1]["system"] == svc.client.requests[0]["system"]


def test_new_plan_starts_new_conversation(data):
    version = {"v": "v1"}
    svc = ChatService(lambda: (data, version["v"]))
    svc._client = FakeClient([([text_block("a")], "end_turn"), ([text_block("b")], "end_turn")])
    cid = list(svc.stream(None, "Q1"))[0]["id"]
    version["v"] = "v2"
    ev = list(svc.stream(cid, "Q2"))[0]
    assert ev["id"] != cid and ev["reset"] is True


def test_tool_schemas_match_validators():
    for t in TOOLS:
        assert t["input_schema"]["additionalProperties"] is False
        req = t["input_schema"]["required"]
        sample = {k: ("T0001" if k == "truck_id" else "LD00001" if k == "load_id" else "Dallas") for k in req}
        assert _validate(t["name"], sample) == sample
