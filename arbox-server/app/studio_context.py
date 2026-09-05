"""Task-owned serialization for active-studio operations and panel requests."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar

from fastapi import HTTPException, Request
from fastapi.routing import APIRoute


_expected_context = ContextVar("arbox_expected_studio", default=None)


def check_identity_change(syncer, target):
    """Reject even a same-task membership refresh that would switch studio."""
    expected = _expected_context.get()
    if (expected and expected[0] is syncer
            and expected[2] is asyncio.current_task() and target != expected[1]):
        raise HTTPException(409, {"code": "studio_changed", "studio_id": syncer.box_id})


class ReentrantAsyncLock:
    """Reentrant only for the owning Task, never for tasks it spawns.

    One lock coordinates sync, booking and automatic ticks. Sharing it avoids
    lock-order inversions when a tick refreshes memberships or an API action
    invokes a tick while protecting the selected studio.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._owner = None
        self._depth = 0

    async def __aenter__(self):
        task = asyncio.current_task()
        if task is not self._owner:
            await self._lock.acquire()
            self._owner = task
        self._depth += 1
        return self

    async def __aexit__(self, *exc):
        if asyncio.current_task() is not self._owner:
            raise RuntimeError("Studio lock released by a different task")
        self._depth -= 1
        if not self._depth:
            self._owner = None
            self._lock.release()


def check_studio(syncer, expected: int):
    if syncer.box_id is None:
        raise HTTPException(409, {"code": "studio_unavailable", "studio_id": None})
    if syncer.box_id != expected:
        raise HTTPException(409, {"code": "studio_changed", "studio_id": syncer.box_id})


class StudioContextRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def guarded(request: Request):
            raw = request.headers.get("X-Arbox-Studio-Id")
            if raw is None:
                return await handler(request)
            # Imported here to avoid the router's module initialization cycle.
            from .api import require_key
            require_key(request, request.headers.get("X-Api-Key"))
            if not raw.isascii() or not raw.isdecimal() or len(raw) > 18 or int(raw) <= 0:
                raise HTTPException(422, "X-Arbox-Studio-Id must be a positive integer")
            expected = int(raw)
            syncer = request.app.state.syncer
            # APIRoute executes the handler in this same task. BaseHTTPMiddleware
            # would run it in a child task and deadlock on nested exclusive().
            async with syncer.exclusive():
                check_studio(syncer, expected)
                token = _expected_context.set((syncer, expected, asyncio.current_task()))
                try:
                    response = await handler(request)
                    check_studio(syncer, expected)
                    response.headers["X-Arbox-Studio-Id"] = str(expected)
                    response.headers["Cache-Control"] = "no-store"
                    return response
                finally:
                    _expected_context.reset(token)

        return guarded
