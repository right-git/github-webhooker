import sys
from loguru import logger

from fastapi import Request
from rich.console import Console
from fastapi.responses import JSONResponse
from app.config.config import settings

logger.remove()
# Add a logger that writes to a file
logger.add(
    "logs/requests.log", enqueue=True, rotation="1 week"
)  # You can specify rotation if needed
# Add a logger that writes to stdout with colorization
logger.add(
    sys.stdout, colorize=True, level="INFO"
)  # Set default console log level to INFO
console = Console()


async def log_middle(request: Request, call_next):
    request_id = request.headers.get(
        "X-Request-ID", "N/A"
    )  # Optional: if you have a request ID header
    logger.info(f"[{request_id}] Incoming Request: {request.method} {request.url.path}")

    if settings.debug:
        logger.debug(f"[{request_id}] Query Params: {request.query_params}")
        if request.path_params:
            logger.debug(f"[{request_id}] Path Params: {request.path_params}")
        logger.debug(f"[{request_id}] Headers:")
        for name, value in request.headers.items():
            logger.debug(f"\t{name}: {value}")

    try:
        response = await call_next(request)
    except Exception as e:
        # Log the full exception with traceback
        console.print_exception(show_locals=True)
        logger.exception(f"[{request_id}] Middleware caught exception: {e}")
        logger.exception(f"[{request_id}] Full traceback:")
        return JSONResponse(
            status_code=500, content={"detail": "Internal Server Error"}
        )

    logger.info(f"[{request_id}] Outgoing Response: {response.status_code}")
    return response
