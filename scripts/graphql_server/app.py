"""FastAPI app serving the marketplace demo:
  - /graphql        GraphQL endpoint + GraphiQL explorer (Module 1 data layer)
  - /chat           POST — one turn through the LangGraph CX agent (Module 3)
  - /analytics      performance dashboard (Module 9) + /analytics.json data
  - /               static chat UI

Run:  python scripts/graphql_server/app.py   (serves on http://127.0.0.1:8000)
The agent's MarketplaceClient calls /graphql over HTTP, so it's a clean service split;
the /chat route is sync, so FastAPI runs it in a threadpool and the self-call won't block
the event loop serving GraphQL.
"""

import sys
from pathlib import Path

# Ensure the local `scripts` package wins over the site-packages one (known shadowing gotcha).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel
from strawberry.fastapi import GraphQLRouter

from scripts.agent.graph import answer
from scripts.agent.tools import AgentTools
from scripts.budget import telemetry_store
from scripts.feedback import capture as feedback_capture
from scripts.feedback import store as feedback_store
from scripts.improvement import analytics
from scripts.graphql_server import db
from scripts.graphql_server.loaders import get_context
from scripts.graphql_server.schema import schema
from scripts.logger import get_logger
from scripts.profile import store as profile_store

logger = get_logger("graphql_server")

db.init_db(include_generated=True)  # canonical seed + generated bulk data (generate_seed.py), if present
profile_store.init_db()  # personalization store (profiles + history) — seeded demo profiles
feedback_store.init_db()  # HITL feedback store (Module 5)
telemetry_store.init_db()  # per-turn telemetry census (Module 6/7/9)

app = FastAPI(title="Marketplace CX — demo")
# context_getter builds fresh per-request DataLoaders (batching / N+1 fix).
app.include_router(GraphQLRouter(schema, context_getter=get_context), prefix="/graphql")

# One shared tool registry (reuses a single MarketplaceClient + loaded config) for all turns.
_TOOLS = AgentTools()
_WEB_DIR = Path(__file__).resolve().parents[1] / "agent" / "web"


class ChatRequest(BaseModel):
    message: str
    buyer_id: str | None = None


@app.post("/chat")
def chat(req: ChatRequest) -> dict:
    state = answer(req.message, buyer_id=req.buyer_id, tools=_TOOLS)
    return {
        "answer": state.get("answer", ""),
        "intent": state.get("intent"),
        "needs_human": state.get("needs_human", False),
        "tools_used": state.get("tool_calls", []),
        "meta": state.get("meta", {}),
    }


class FeedbackRequest(BaseModel):
    signal_type: str                 # rating | correction | edit | escalation
    turn_id: int | None = None
    buyer_id: str | None = None
    intent: str | None = None
    rating: int | None = None        # +1 / -1 for signal_type="rating"
    correction: str | None = None
    edit: str | None = None


@app.post("/feedback")
def feedback(req: FeedbackRequest) -> dict:
    """External capture channel: an end-user thumbs/correction/edit for a turn that happened."""
    fid = feedback_capture.submit(
        req.signal_type, turn_id=req.turn_id, buyer_id=req.buyer_id, intent=req.intent,
        reviewer_id="user", rating=req.rating, correction=req.correction, edit=req.edit,
    )
    return {"feedback_id": fid, "status": "recorded"}


@app.get("/analytics.json")
def analytics_data() -> dict:
    """Module 9: live performance/cost metrics per module (telemetry census + feedback sample)."""
    return analytics.build_dashboard()


@app.get("/analytics")
def analytics_page() -> FileResponse:
    return FileResponse(_WEB_DIR / "analytics.html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def home() -> FileResponse:
    return FileResponse(_WEB_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting marketplace demo on http://127.0.0.1:8000  (chat UI at /, GraphQL at /graphql)")
    uvicorn.run(app, host="127.0.0.1", port=8000)
