# CRITICAL: Initialize telemetry BEFORE importing AI frameworks
from sap_cloud_sdk.aicore import set_aicore_config
from sap_cloud_sdk.core.telemetry import auto_instrument
import os

# ── Cap OTLP exporter retries BEFORE auto_instrument() initialises them ──────
# When the OTLP collector is unreachable the default exporters retry with
# exponential back-off indefinitely, flooding logs with
# "Transient error Service Unavailable … retrying in Xs" messages.
# Setting these env-vars to conservative values makes each exporter give up
# quickly so the agent stays responsive even without a working collector.
#
#   OTEL_EXPORTER_OTLP_TIMEOUT       — per-attempt timeout in milliseconds (default 10 000)
#   OTEL_EXPORTER_OTLP_RETRY_MAX_ELAPSED — total wall-clock budget for all retries (default 120 000)
#
# Only set defaults here; explicit values in the container environment win.
os.environ.setdefault("OTEL_EXPORTER_OTLP_TIMEOUT", "3000")           # 3 s per attempt
os.environ.setdefault("OTEL_EXPORTER_OTLP_RETRY_MAX_ELAPSED", "8000") # 8 s total, then drop

set_aicore_config()
auto_instrument()

import logging
import os

import click
import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from starlette.middleware.base import BaseHTTPMiddleware

from agent_executor import AgentExecutor
from mcp_tools import set_user_token
from opentelemetry.instrumentation.starlette import StarletteInstrumentor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5000"))


class JWTContextMiddleware(BaseHTTPMiddleware):
    """Middleware that extracts JWT token from Authorization header and sets it in context."""

    async def dispatch(self, request, call_next):
        # Extract JWT token from Authorization header
        auth_header = request.headers.get("authorization", "")
        token = None
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:]  # Remove "Bearer " prefix

        # Set the token in the context variable
        set_user_token(token)

        try:
            response = await call_next(request)
            return response
        finally:
            # Clear the token after the request
            set_user_token(None)


@click.command()
@click.option("--host", default=HOST)
@click.option("--port", default=PORT)
def main(host: str, port: int):
    skill = AgentSkill(
        id="sf-payroll-agent",
        name="sf-payroll-agent",
        description="An AI agent for SAP SuccessFactors payroll operations: natural language queries, anomaly detection, tax compliance validation, and compensation management",
        tags=["sf", "payroll", "successfactors", "agent"],
        examples=["Show me the payroll results for employee 12345 for June 2025", "Detect payroll anomalies for the last payroll run", "Validate tax compliance for employee 67890 in fiscal year 2025", "What is the current salary for employee 12345?"],
    )
    agent_card = AgentCard(
        name="sf-payroll-agent",
        description="An AI agent for SAP SuccessFactors payroll operations: natural language queries, anomaly detection, tax compliance validation, and compensation management",
        url=os.environ.get("AGENT_PUBLIC_URL", f"http://{host}:{port}/"),
        version="1.0.0",
        default_input_modes=["text", "text/plain"],
        default_output_modes=["text", "text/plain"],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        skills=[skill],
    )
    server = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=DefaultRequestHandler(
            agent_executor=AgentExecutor(),
            task_store=InMemoryTaskStore(),
        ),
    )
    app = server.build()

    # Add JWT context middleware
    app.add_middleware(JWTContextMiddleware)

    StarletteInstrumentor().instrument_app(app)

    logger.info(f"Starting A2A server at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
