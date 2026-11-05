from fastapi import FastAPI

from cmp_api.routes.agents import router as agent_router
from cmp_api.routes.approvals import router as approval_router
from cmp_api.routes.connections import router as connection_router
from cmp_api.routes.env_files import router as env_file_router
from cmp_api.routes.migrations import router as migration_router
from cmp_api.routes.onprem_rollout import router as rollout_router
from cmp_api.routes.plan_inputs import router as plan_input_router
from cmp_api.routes.tunnels import router as tunnel_router

app = FastAPI(title="Cloud Migration Platform", version="0.1.0")
app.include_router(migration_router)
app.include_router(agent_router)
app.include_router(approval_router)
app.include_router(rollout_router)
app.include_router(tunnel_router)
app.include_router(plan_input_router)
app.include_router(connection_router)
app.include_router(env_file_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
