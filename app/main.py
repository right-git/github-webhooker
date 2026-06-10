from loguru import logger
from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.middlewares.log import log_middle
from app.config.config import settings
from app.routers import webhook


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up...")

    try:
        yield
    finally:
        logger.info("Shutting down...")


app = FastAPI(
    lifespan=lifespan, docs_url=settings.docs_url, redoc_url=settings.redoc_url
)

# Add the middleware to the app
app.add_middleware(BaseHTTPMiddleware, dispatch=log_middle)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTION", "DELETE", "PUT", "PATCH", "HEAD"],
    allow_headers=["*"],
)


# Admin routes
app.include_router(webhook.router)