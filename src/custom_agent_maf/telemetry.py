"""Application Insights / OpenTelemetry instrumentation for the custom MAF agent.

Self-contained copy for the custom MAF agent (no shared modules). Tracing is
best-effort: if the connection string or the optional packages are missing the
agent still runs, just without traces.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def configure_telemetry(logger_name: str = "weather_agent_maf") -> None:
    """Wire Azure Monitor + agent-framework observability when configured.

    Reads ``APPLICATIONINSIGHTS_CONNECTION_STRING`` from the environment (the
    ACA deploy script passes it for the custom container).
    """
    conn = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if not conn:
        logger.info("APPLICATIONINSIGHTS_CONNECTION_STRING not set — tracing disabled")
        return

    # Prefer the agent-framework helper: it sets up OTel and enables GenAI
    # instrumentation for agent/tool spans, exporting to Azure Monitor.
    try:
        from agent_framework.observability import setup_observability

        setup_observability(applicationinsights_connection_string=conn)
        logging.getLogger("azure").setLevel(logging.WARNING)
        logger.info("agent-framework observability configured (Application Insights)")
        return
    except Exception:  # noqa: BLE001 - fall back to plain Azure Monitor
        logger.debug("agent-framework observability unavailable; falling back", exc_info=True)

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(connection_string=conn, logger_name=logger_name)
        logging.getLogger("azure").setLevel(logging.WARNING)
        logger.info("Azure Monitor OpenTelemetry configured")
    except Exception:  # noqa: BLE001 - tracing must never break the agent
        logger.warning("Could not configure Application Insights instrumentation", exc_info=True)
