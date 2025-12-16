"""
FastAPI entrypoint for the Policy & Risk Guardrails PoC.
- Loads settings
- Configures CORS
- Mounts /policy routes
- Exposes simple health endpoints
"""

from __future__ import annotations

import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.routers.policy import router as policy_router
from app.routers import policy_config
from app.routers import policy_override
from app.routers import policy_audit




def create_app() -> FastAPI:
    app = FastAPI(
        title="Policy & Risk Guardrails",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Logging
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # CORS
    if settings.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Routers
    app.include_router(policy_router)
    app.include_router(policy_config.router)
    app.include_router(policy_override.router)
    app.include_router(policy_audit.router) 
    
    # Health
    @app.get("/health", tags=["_system"])
    async def health():
        return {"status": "ok", "env": settings.APP_ENV, "tz": settings.APP_TIMEZONE}

    @app.get("/ready", tags=["_system"])
    async def ready():
        # In a real service you might check DB connectivity here.
        return {"ready": True}

    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
app = create_app()