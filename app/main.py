"""Xiaoduan Studio V3 default application entrypoint.

The former AI Studio V2 / Stage01-04 monolith has been retired from the active
runtime. Historical migration artifacts remain in git history/deliverables,
but the default ASGI application is V3 only.
"""

from app.v3.main import app

__all__ = ["app"]
