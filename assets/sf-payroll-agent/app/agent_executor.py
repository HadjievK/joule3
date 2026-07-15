import logging
import os

from a2a.server.agent_execution import AgentExecutor as A2AAgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    InternalError,
    Part,
    TaskState,
    TextPart,
    UnsupportedOperationError,
)
from a2a.utils import new_agent_text_message, new_task
from a2a.utils.errors import ServerError

from agent import SampleAgent, get_system_prompt
from agentmesh_integration import run_startup_checks, wrap_tools_with_trust_verification, append_audit_entry
from governance import apply_governance, verify_owasp_coverage
from load_skill_resources import get_load_skill_resource_tool
from mcp_tools import get_mcp_tools, get_user_token
from tool_integrity import ToolIntegrityGuard
from tools.manage_compensation import (
    build_update_salary_tool,
    build_update_bonus_eligibility_tool,
)

logger = logging.getLogger(__name__)

# ── One-time startup checks (run at import = agent process start) ────────────
_OWASP_COVERAGE = verify_owasp_coverage()
_STARTUP_RESULTS = run_startup_checks(system_prompt=get_system_prompt())


class AgentExecutor(A2AAgentExecutor):
    def __init__(self):
        self.agent = SampleAgent()
        self.skill_tools = get_load_skill_resource_tool()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Execute the agent and stream results back via A2A protocol.

        Per-request flow:
          1. Load MCP tools from Agent Gateway (user-scoped credentials).
          2. Attach confirmation-gated write tools (salary + bonus eligibility).
          3. Apply AGT governance wrapper to ALL tools — PII detection, prompt
             injection guards, salary cap rules, and approval gates.
          4. Invoke the LangChain agent graph and stream results.

        Args:
            context: Request context containing user input and task info
            event_queue: Queue for publishing task status updates

        Raises:
            ServerError: On unrecoverable agent execution errors
        """
        query = context.get_user_input()
        task = context.current_task
        if not task:
            task = new_task(context.message)
            await event_queue.enqueue_event(task)

        # Get user token from context variable (set by middleware)
        user_token = get_user_token()

        # ── Step 1: Load MCP tools ───────────────────────────────────────────
        tools = []
        try:
            tools = await get_mcp_tools(user_token=user_token)
            if not tools:
                logger.warning("No tools returned from Agent Gateway")
            else:
                tool_names = [t.name for t in tools]
                logger.info("Loaded %d MCP tool(s): %s", len(tools), tool_names)
        except ValueError as e:
            logger.error("Invalid user token: %s", e)
        except Exception as e:
            logger.error("Failed to load tools from Agent Gateway: %s", e)

        # ── Step 2: Attach write tools ───────────────────────────────────────
        write_tools = [
            build_update_salary_tool(tools),
            build_update_bonus_eligibility_tool(tools),
        ]

        # ── Step 2a: Content-hash the write tools for tamper detection ───────
        # Builds (or reuses) a ToolIntegrityGuard that hashes each write tool's
        # source code at startup. Before each write call the hash is recomputed
        # and compared — any wrapper injection or monkey-patching is caught here.
        integrity_guard = ToolIntegrityGuard.from_tools(write_tools, strict=True)
        logger.info("tool_integrity: %s", integrity_guard.summary())

        # ── Step 2b: Wrap write tools with agentmesh trust verification ──────
        # Downstream agents calling write tools must have trust_score >= 600.
        # Falls back to no-op when agentmesh is not installed.
        write_tools = wrap_tools_with_trust_verification(write_tools)

        tools = [*tools, *write_tools, *self.skill_tools]

        # ── Step 3: Apply AGT governance to ALL tools ────────────────────────
        # Pass the original user query so text-matching rules (PII, prompt
        # injection, bulk-write detection) can evaluate the raw input.
        tools = apply_governance(tools, input_text=query)

        # ── Step 4: Stream agent execution ──────────────────────────────────
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        try:
            async for item in self.agent.stream(query, task.context_id, tools=tools):
                is_task_complete = item["is_task_complete"]
                require_user_input = item["require_user_input"]
                content = item["content"]

                if require_user_input:
                    await updater.update_status(
                        TaskState.input_required,
                        new_agent_text_message(content, task.context_id, task.id),
                        final=True,
                    )
                    break
                elif is_task_complete:
                    await updater.add_artifact(
                        [Part(root=TextPart(text=content))], name="agent_result"
                    )
                    await updater.complete()
                    break
                else:
                    await updater.update_status(
                        TaskState.working,
                        new_agent_text_message(content, task.context_id, task.id),
                    )
        except Exception as e:
            logger.exception("Agent execution error")
            raise ServerError(error=InternalError()) from e

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ServerError(error=UnsupportedOperationError())
