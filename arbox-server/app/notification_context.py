"""Task-local notification routing, inherited by delayed delivery tasks."""
from contextvars import ContextVar
from functools import wraps
from inspect import signature

notification_kind = ContextVar('notification_kind', default='system')
health_notice = ContextVar('health_notice', default=False)


def routed(function):
    sig = signature(function)
    @wraps(function)
    async def wrapper(*args, **kwargs):
        kind = sig.bind(*args, **kwargs).arguments.get('kind', 'system')
        token = notification_kind.set(kind)
        try:
            return await function(*args, **kwargs)
        finally:
            notification_kind.reset(token)
    return wrapper
