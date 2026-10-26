"""Redis-backed idempotency for the order intake endpoint.

Idempotency is enforced twice, on purpose:

* PostgreSQL refuses a second row for the same ``order_ref`` (the durable
  guarantee, see ``POST /orders``).
* This middleware replays the original HTTP response for a repeated
  ``X-Idempotency-Key``, which also covers clients that let the service generate
  the reference.
"""

import asyncio
import hashlib
import json
import time

import redis.asyncio as aioredis
import structlog
from fastapi import Request, status
from redis.exceptions import RedisError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from order.app.config import get_order_settings

logger = structlog.get_logger("order-idempotency")

settings = get_order_settings()

IDEMPOTENCY_HEADER = "X-Idempotency-Key"
REPLAY_HEADER = "X-Idempotent-Replay"
IDEMPOTENCY_PATH = "/orders"

STATE_PROCESSING = "PROCESSING"
STATE_COMPLETED = "COMPLETED"


def _is_replayable(status_code: int) -> bool:
    """5xx (including 503) is transient, so the client may retry with the same key."""
    return status_code < 500 and status_code != status.HTTP_503_SERVICE_UNAVAILABLE


class RedisIdempotencyMiddleware(BaseHTTPMiddleware):
    """Deduplicate concurrent or retried order submissions via Redis."""

    def __init__(self, app, redis_url: str) -> None:
        super().__init__(app)
        self.redis = aioredis.from_url(redis_url, decode_responses=True)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Only mutations need protecting; reads pass straight through.
        if request.method != "POST" or request.url.path != IDEMPOTENCY_PATH:
            return await call_next(request)

        idempotency_key = request.headers.get(IDEMPOTENCY_HEADER)
        if not idempotency_key:
            # The header is optional: a client-supplied order_ref is already
            # deduplicated by the primary key. Without either, two identical
            # submissions are two legitimate orders.
            return await call_next(request)

        redis_key = f"idempotency:{idempotency_key}"
        request_hash = await self._request_hash(request)

        try:
            claimed = await self._claim(redis_key, request_hash)
        except RedisError as exc:
            # Fail open: the database constraint still prevents double counting.
            logger.error("Idempotency cache unavailable; processing request", error=str(exc))
            return await call_next(request)

        if not claimed:
            return await self._replay(redis_key, request_hash, request, call_next)

        return await self._execute(redis_key, request_hash, request, call_next)

    # --- claiming and executing ------------------------------------------ #

    async def _claim(self, redis_key: str, request_hash: str) -> bool:
        """Atomically become the owner of a key. Raises RedisError if unavailable."""
        claimed = await self.redis.set(
            redis_key,
            json.dumps({"state": STATE_PROCESSING, "request_hash": request_hash}),
            nx=True,
            ex=settings.IDEMPOTENCY_EXPIRE_SECONDS,
        )
        return bool(claimed)

    async def _execute(
        self,
        redis_key: str,
        request_hash: str,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Run the handler once and cache the response for replays."""
        try:
            response = await call_next(request)
            body = await self._consume_body(response)

            if body and _is_replayable(response.status_code):
                await self._remember(redis_key, request_hash, response, body)
            else:
                # Nothing worth replaying; release the key so the client may retry.
                await self._forget(redis_key)

            return self._rebuild(response, body)

        except Exception:
            # Release the key so the client can retry after a genuine failure.
            await self._forget(redis_key)
            logger.exception("Order request failed; idempotency key released")
            raise

    async def _replay(
        self,
        redis_key: str,
        request_hash: str,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Wait for the in-flight original, then replay its exact response."""
        deadline = time.monotonic() + settings.IDEMPOTENCY_WAIT_SECONDS

        while time.monotonic() < deadline:
            entry = await self._read_entry(redis_key)

            if entry is None:
                # The original failed and released the key. Try to take it over
                # instead of reporting a conflict the client cannot act on.
                if await self._claim(redis_key, request_hash):
                    return await self._execute(redis_key, request_hash, request, call_next)
                await asyncio.sleep(0.05)
                continue

            if entry.get("state") == STATE_COMPLETED and "body" in entry:
                if entry.get("request_hash") not in (None, request_hash):
                    return self._conflict(
                        "This X-Idempotency-Key was already used for a different order payload."
                    )

                logger.info("Replaying idempotent response")
                return Response(
                    content=entry["body"],
                    status_code=entry["status_code"],
                    media_type="application/json",
                    headers={REPLAY_HEADER: "true"},
                )

            await asyncio.sleep(0.05)

        return self._conflict(
            "A duplicate request with this key is still processing; retry shortly."
        )

    # --- helpers --------------------------------------------------------- #

    @staticmethod
    async def _request_hash(request: Request) -> str:
        """Fingerprint the request so a key cannot be reused for a different cart."""
        body = await request.body()
        digest = hashlib.sha256()
        digest.update(request.method.encode())
        digest.update(request.url.path.encode())
        digest.update(body)
        return digest.hexdigest()

    @staticmethod
    async def _consume_body(response: Response) -> bytes:
        """Read the response body so it can be cached and re-emitted."""
        body = b""
        body_iterator = getattr(response, "body_iterator", None)
        if body_iterator is not None:
            async for chunk in body_iterator:
                body += chunk if isinstance(chunk, bytes) else chunk.encode()
        else:
            body = bytes(response.body)
        return body

    @staticmethod
    def _rebuild(response: Response, body: bytes) -> Response:
        headers = {
            name: value
            for name, value in response.headers.items()
            if name.lower() not in {"content-length", "content-encoding"}
        }
        return Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )

    async def _remember(
        self,
        redis_key: str,
        request_hash: str,
        response: Response,
        body: bytes,
    ) -> None:
        try:
            await self.redis.set(
                redis_key,
                json.dumps(
                    {
                        "state": STATE_COMPLETED,
                        "request_hash": request_hash,
                        "status_code": response.status_code,
                        "body": body.decode(),
                    }
                ),
                ex=settings.IDEMPOTENCY_EXPIRE_SECONDS,
            )
        except RedisError as exc:
            logger.warning("Could not cache idempotent response", error=str(exc))

    async def _forget(self, redis_key: str) -> None:
        try:
            await self.redis.delete(redis_key)
        except RedisError as exc:
            logger.warning("Could not release idempotency key", error=str(exc))

    async def _read_entry(self, redis_key: str) -> dict | None:
        try:
            raw = await self.redis.get(redis_key)
        except RedisError as exc:
            logger.warning("Could not read idempotency key", error=str(exc))
            return None

        if raw is None:
            return None

        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            return None

        return entry if isinstance(entry, dict) else None

    @staticmethod
    def _conflict(detail: str) -> Response:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": detail},
        )
