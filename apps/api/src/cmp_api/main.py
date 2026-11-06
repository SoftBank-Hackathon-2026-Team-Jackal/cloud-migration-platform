import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cmp_api.config import settings
from cmp_api.routes.agent_jobs import router as agent_job_router
from cmp_api.routes.agents import router as agent_router
from cmp_api.routes.approvals import router as approval_router
from cmp_api.routes.connections import router as connection_router
from cmp_api.routes.env_files import router as env_file_router
from cmp_api.routes.migrations import router as migration_router
from cmp_api.routes.onprem_rollout import router as rollout_router
from cmp_api.routes.plan_inputs import router as plan_input_router
from cmp_api.routes.tunnels import router as tunnel_router

logger = logging.getLogger(__name__)


def sync_connection_profiles(session_factory) -> None:
    """Ticket 46 (B-03, N12): rewrite the shared connection AWS config from the DB at start-up.

    The worker reads the same file (`AWS_CONFIG_FILE`), so profiles survive a redeploy or a new
    base file. No-op (and no DB access) when the file is not configured.
    """
    if not settings.connection_aws_config_file or not settings.connection_platform_aws_profile:
        return
    from sqlalchemy.exc import SQLAlchemyError

    from cmp_api.connections import sync_aws_config

    try:
        with session_factory() as session:
            sync_aws_config(
                session,
                path=settings.connection_aws_config_file,
                base_path=settings.connection_aws_base_config_file,
                platform_profile=settings.connection_platform_aws_profile,
            )
    except SQLAlchemyError:
        # The API still serves; the next role registration or deletion rewrites the file.
        logger.warning("connection profile sync skipped: business DB unavailable")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from cmp_api.db import SessionLocal

    sync_connection_profiles(SessionLocal)
    yield


app = FastAPI(title="Cloud Migration Platform", version="0.1.0", lifespan=lifespan)
app.include_router(migration_router)
app.include_router(agent_router)
app.include_router(agent_job_router)
app.include_router(approval_router)
app.include_router(rollout_router)
app.include_router(tunnel_router)
app.include_router(plan_input_router)
app.include_router(connection_router)
app.include_router(env_file_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
