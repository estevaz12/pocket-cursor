"""Optional Sentry error reporting, Prometheus metrics, and OpenTelemetry tracing hooks."""

from __future__ import annotations

import os

import prometheus_client  # noqa: F401 — metrics registry for operators
import sentry_sdk
from opentelemetry import trace

TRACER = trace.get_tracer(__name__)


def init_sentry() -> None:
    dsn = os.environ.get('SENTRY_DSN', '').strip()
    if not dsn:
        return
    sentry_sdk.init(dsn=dsn, send_default_pii=False)
