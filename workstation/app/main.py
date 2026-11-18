"""Application entry: `uvicorn app.main:app`."""

from app.core.setup import create_application
from app.routers import api_router

app = create_application()
app.include_router(api_router)

__all__ = ["app"]
