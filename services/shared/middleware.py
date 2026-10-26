import asyncio
import random

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from shared.config import get_shared_settings

settings = get_shared_settings()


class ChaosMonkeyMiddleware(BaseHTTPMiddleware):
    """
    Chaos Monkey middleware for simulating failures in the application.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        """
        Dispatch the request to the next middleware in the chain.
        """
        if request.url.path == "/health":
            return await call_next(request)
        # Roll the dice to simulate the glitch.
        if settings.ENABLE_GLITCH_SIMULATION and random.random() < settings.GLITCH_PROBABILITY:
            glitch_type = random.choice(["delay", "server_error"])

            if glitch_type == "delay":
                delay = random.uniform(0.5, settings.GLITCH_MAX_DELAY_SECONDS)
                # Simulate transient latency network drops.
                await asyncio.sleep(delay)
            elif glitch_type == "server_error":
                # Simulate a sudden downstreamnetwork break or node drop.
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Service Unavailable: Temporary Network Outage [Simulated]",
                )
        # Proceed safely to downstream endpoint if no glitch triggered.
        return await call_next(request)
