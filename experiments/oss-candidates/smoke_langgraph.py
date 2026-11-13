"""Isolated LangGraph API smoke test. Synthetic data, no Imperial imports."""
from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class DevelopmentState(TypedDict):
    task_id: str
    owner_approved: bool
    status: str


def check_approval(state: DevelopmentState) -> dict:
    """Never advance an unapproved synthetic development task."""
    return {
        "status": "approved_for_development" if state["owner_approved"] else "review_needed"
    }


builder = StateGraph(DevelopmentState)
builder.add_node("check_approval", check_approval)
builder.add_edge(START, "check_approval")
builder.add_edge("check_approval", END)
graph = builder.compile()

unapproved = graph.invoke(
    {"task_id": "synthetic-unapproved", "owner_approved": False, "status": "queued"}
)
assert unapproved["status"] == "review_needed", unapproved

approved = graph.invoke(
    {"task_id": "synthetic-approved", "owner_approved": True, "status": "queued"}
)
assert approved["status"] == "approved_for_development", approved

assert unapproved["task_id"] == "synthetic-unapproved"
assert approved["task_id"] == "synthetic-approved"
print("LangGraph smoke PASS: deterministic approval gate on synthetic tasks")
