from fastapi import FastAPI

from cmp_api.routes.agents import router as agent_router
from cmp_api.routes.migrations import router as migration_router

app = FastAPI(title="Cloud Migration Platform", version="0.1.0")
app.include_router(migration_router)
app.include_router(agent_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
