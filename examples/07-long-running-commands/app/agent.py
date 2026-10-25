"""ADK Workflow Graph for Long-Running gcloud Commands (--async) & Verification.

Implements a 7-node ADK `Workflow` graph (`from google.adk.workflow import Workflow`)
with a deterministic conditional polling loop connected to
`https://cloudcli.googleapis.com/mcp`:

  1. `init_lro_context` (`FunctionNode`):
     - Initializes state (`target_project`, `default_zone`, `target_vm_name`,
       `desired_action` ["start" | "stop"], `expected_vm_status` ["RUNNING" | "TERMINATED"],
       `poll_attempt=0`, `max_poll_attempts=10`, `poll_interval_seconds=5`).

  2. `submit_async_gcloud_agent` (`LlmAgent` + `McpToolset`):
     - Calls `run_gcloud_command` with `--async --format=json --quiet`:
       `gcloud compute instances {desired_action} {target_vm_name} --zone={default_zone} --project={target_project} --async --format=json --quiet`
     - Saves the returned Google Cloud Operation metadata to `state["async_submission_result"]`.

  3. `extract_operation_metadata` (`FunctionNode`):
     - Deterministically extracts `operation_name` (`operation-...`) from
       `async_submission_result`, saves it to `state["operation_name"]`, and
       waits `poll_interval_seconds` before the first poll.

  4. `poll_operation_agent` (`LlmAgent` + `McpToolset`):
     - Calls `run_gcloud_command` to inspect the Long-Running Operation (LRO):
       `gcloud compute operations describe {operation_name} --zone={default_zone} --project={target_project} --format=json --quiet`
     - Writes the operation status JSON to `state["latest_operation_poll"]`.

  5. `evaluate_poll_status` (`FunctionNode` — Conditional Loop Router):
     - Increments `poll_attempt` and inspects the operation status:
       - `route="continue"`: Operation is still `PENDING` or `RUNNING` and
         `poll_attempt < max_poll_attempts` -> sleeps `poll_interval_seconds`
         and loops back to `poll_operation_agent`.
       - `route="done"`: Operation reached `status == "DONE"` without error ->
         exits the loop to `verify_resource_state_agent`.
       - `route="failed"`: Operation returned an error or exceeded `max_poll_attempts` ->
         routes to `handle_lro_failure`.

  6. `verify_resource_state_agent` (`LlmAgent` + `McpToolset`, route="done"):
     - Calls `run_gcloud_command` (`gcloud compute instances describe ...`) to
       verify that `{target_vm_name}` actually transitioned to `{expected_vm_status}`
       (`RUNNING` or `TERMINATED`), saving the final JSON verification report to
       `state["resource_verification_result"]`.

  7. `handle_lro_failure` (`FunctionNode`, route="failed"):
     - Records a structured failure summary if the LRO failed or timed out.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.events.event import Event
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)
from google.adk.workflow import Workflow

DEFAULT_PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "YOUR_PROJECT_ID")
DEFAULT_ZONE = os.environ.get("GOOGLE_CLOUD_ZONE", "us-central1-a")
DEFAULT_VM_NAME = os.environ.get("TARGET_VM_NAME", "demo-web-server")
DEFAULT_ACTION = os.environ.get("LRO_ACTION", "start")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
GCLOUD_MCP_URL = "https://cloudcli.googleapis.com/mcp"


def get_dynamic_mcp_headers(ctx: Any = None) -> dict[str, str]:
    """Refreshes Application Default Credentials (ADC) for each MCP request."""
    import google.auth
    import google.auth.transport.requests

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    quota_project = os.environ.get("GOOGLE_CLOUD_PROJECT", DEFAULT_PROJECT_ID)
    if ctx is not None and hasattr(ctx, "state"):
        quota_project = ctx.state.get("target_project", quota_project)
    return {
        "Authorization": f"Bearer {credentials.token}",
        "x-goog-user-project": str(quota_project),
    }


gcloud_mcp_tools = McpToolset(
    connection_params=StreamableHTTPConnectionParams(
        url=GCLOUD_MCP_URL,
        timeout=10.0,
        sse_read_timeout=300.0,
    ),
    header_provider=get_dynamic_mcp_headers,
    tool_filter=["run_gcloud_command"],
)


def _parse_json_dict(raw_text: str) -> dict[str, Any]:
    """Extracts a JSON dict from LLM text output (handling markdown fences or arrays)."""
    cleaned = str(raw_text or "").strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            return parsed[0]
    except json.JSONDecodeError:
        pass

    obj_match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if obj_match:
        try:
            parsed = json.loads(obj_match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


# ---------------------------------------------------------------------------
# Node 1: Initialize Long-Running Operation Context (FunctionNode)
# ---------------------------------------------------------------------------
def init_lro_context(node_input: str) -> Event:
    """Initializes workflow state for an asynchronous gcloud VM start/stop operation."""
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", DEFAULT_PROJECT_ID)
    zone = os.environ.get("GOOGLE_CLOUD_ZONE", DEFAULT_ZONE)
    vm_name = os.environ.get("TARGET_VM_NAME", DEFAULT_VM_NAME)

    prompt_lower = str(node_input or "").lower()
    env_action = os.environ.get("LRO_ACTION", DEFAULT_ACTION).lower().strip()
    if "stop" in prompt_lower:
        desired_action = "stop"
    elif "start" in prompt_lower:
        desired_action = "start"
    else:
        desired_action = "stop" if env_action == "stop" else "start"

    expected_vm_status = "RUNNING" if desired_action == "start" else "TERMINATED"
    poll_interval_seconds = int(os.environ.get("LRO_POLL_INTERVAL_SECONDS", "5"))
    max_poll_attempts = int(os.environ.get("LRO_MAX_POLL_ATTEMPTS", "10"))

    state_init = {
        "target_project": project_id,
        "default_zone": zone,
        "target_vm_name": vm_name,
        "desired_action": desired_action,
        "expected_vm_status": expected_vm_status,
        "poll_attempt": 0,
        "max_poll_attempts": max_poll_attempts,
        "poll_interval_seconds": poll_interval_seconds,
        "user_request": str(node_input).strip(),
    }
    return Event(
        output=(
            f"Submit asynchronous 'gcloud compute instances {desired_action}' "
            f"for VM '{vm_name}' in zone '{zone}' (project '{project_id}')."
        ),
        state=state_init,
    )


# ---------------------------------------------------------------------------
# Node 2: Submit Long-Running Command with --async (LlmAgent)
# ---------------------------------------------------------------------------
submit_async_gcloud_agent = LlmAgent(
    name="submit_async_gcloud_agent",
    model=os.environ.get("ADK_MODEL", "gemini-3-flash-preview"),
    include_contents="none",
    description="Submits a long-running gcloud compute instances start/stop command with --async.",
    instruction="""You are the Async Command Submission Node in an ADK Workflow.

Workflow Context:
- Target Project: {target_project}
- Target Zone: {default_zone}
- Target VM Name: {target_vm_name}
- Desired Action: {desired_action}

Task:
1. Call `run_gcloud_command` once with:
   - `project`: "projects/{target_project}"
   - `command`: "gcloud compute instances {desired_action} {target_vm_name} --zone={default_zone} --project={target_project} --async --format=json --quiet"
2. Output ONLY a valid JSON object containing:
   - "operation_name": string (the short operation name, e.g. "operation-1792...")
   - "operation_type": string (e.g. "start" or "stop")
   - "initial_status": string (e.g. "RUNNING" or "PENDING" or "DONE")
   - "progress": integer (e.g. 0 or 100)
   - "target_vm_name": "{target_vm_name}"
   Do not wrap the JSON in conversational prose.
""",
    tools=[gcloud_mcp_tools],
    output_key="async_submission_result",
)


# ---------------------------------------------------------------------------
# Node 3: Extract Operation Name & Wait Before First Poll (FunctionNode)
# ---------------------------------------------------------------------------
async def extract_operation_metadata(
    node_input: Any,
    async_submission_result: str = "",
    poll_interval_seconds: int = 5,
    target_project: str = DEFAULT_PROJECT_ID,
    default_zone: str = DEFAULT_ZONE,
) -> Event:
    """Extracts the Google Cloud operation name (operation-...) and pauses briefly before polling."""
    raw_text = str(async_submission_result or node_input or "")
    parsed = _parse_json_dict(raw_text)

    operation_name = str(
        parsed.get("operation_name") or parsed.get("name") or ""
    ).strip()
    if "/" in operation_name:
        operation_name = operation_name.rsplit("/", 1)[-1]

    if not operation_name:
        op_match = re.search(r"(operation-[0-9a-zA-Z-]+)", raw_text)
        if op_match:
            operation_name = op_match.group(1)

    if not operation_name:
        raise ValueError(
            f"Could not extract operation_name from async_submission_result: {raw_text!r}"
        )

    initial_status = str(
        parsed.get("initial_status") or parsed.get("status") or "RUNNING"
    ).upper()

    await asyncio.sleep(max(1, int(poll_interval_seconds)))

    state_delta = {
        "operation_name": operation_name,
        "operation_initial_status": initial_status,
    }
    return Event(
        output=(
            f"Describe Compute Engine operation '{operation_name}' in zone "
            f"'{default_zone}' for project '{target_project}'."
        ),
        state=state_delta,
    )


# ---------------------------------------------------------------------------
# Node 4: Poll Operation Status via Remote MCP (LlmAgent)
# ---------------------------------------------------------------------------
poll_operation_agent = LlmAgent(
    name="poll_operation_agent",
    model=os.environ.get("ADK_MODEL", "gemini-3-flash-preview"),
    include_contents="none",
    description="Polls a Compute Engine Long-Running Operation via gcloud compute operations describe.",
    instruction="""You are the LRO Polling Node in an ADK Workflow.

Workflow Context:
- Target Project: {target_project}
- Target Zone: {default_zone}
- Operation Name: {operation_name}
- Current Poll Attempt: {poll_attempt}

Task:
1. Call `run_gcloud_command` once with:
   - `project`: "projects/{target_project}"
   - `command`: "gcloud compute operations describe {operation_name} --zone={default_zone} --project={target_project} --format=json --quiet"
2. Output ONLY a valid JSON object containing:
   - "operation_name": "{operation_name}"
   - "status": string (exact operation status from gcloud, e.g. "PENDING", "RUNNING", or "DONE")
   - "progress": integer (0 to 100)
   - "operation_type": string (e.g. "start" or "stop")
   - "error": null (or the error object if the operation failed)
   Do not wrap the JSON in conversational prose.
""",
    tools=[gcloud_mcp_tools],
    output_key="latest_operation_poll",
)


# ---------------------------------------------------------------------------
# Node 5: Evaluate Poll Status & Route Loop (FunctionNode)
# ---------------------------------------------------------------------------
async def evaluate_poll_status(
    node_input: Any,
    latest_operation_poll: str = "",
    operation_name: str = "",
    poll_attempt: int = 0,
    max_poll_attempts: int = 10,
    poll_interval_seconds: int = 5,
    target_vm_name: str = DEFAULT_VM_NAME,
    expected_vm_status: str = "RUNNING",
) -> Event:
    """Evaluates whether the LRO is DONE, needs another poll ('continue'), or failed ('failed')."""
    current_attempt = int(poll_attempt) + 1
    raw_text = str(latest_operation_poll or node_input or "")
    parsed = _parse_json_dict(raw_text)

    op_status = str(parsed.get("status", "UNKNOWN")).upper()
    if op_status == "UNKNOWN":
        if '"DONE"' in raw_text or "'DONE'" in raw_text:
            op_status = "DONE"
        elif '"RUNNING"' in raw_text or "'RUNNING'" in raw_text:
            op_status = "RUNNING"
        elif '"PENDING"' in raw_text or "'PENDING'" in raw_text:
            op_status = "PENDING"

    progress = parsed.get("progress", 100 if op_status == "DONE" else 0)
    op_error = parsed.get("error")

    if op_error:
        state_delta = {
            "poll_attempt": current_attempt,
            "operation_final_status": "ERROR",
            "operation_error": op_error,
            "lro_completed": False,
        }
        return Event(
            output={
                "route_selected": "failed",
                "operation_name": operation_name,
                "poll_attempt": current_attempt,
                "error": op_error,
            },
            route="failed",
            state=state_delta,
        )

    if op_status == "DONE":
        state_delta = {
            "poll_attempt": current_attempt,
            "operation_final_status": "DONE",
            "operation_progress": 100,
            "lro_completed": True,
        }
        return Event(
            output=(
                f"Operation '{operation_name}' is DONE after {current_attempt} poll(s). "
                f"Now verify that VM '{target_vm_name}' is in '{expected_vm_status}' state."
            ),
            route="done",
            state=state_delta,
        )

    if current_attempt >= int(max_poll_attempts):
        state_delta = {
            "poll_attempt": current_attempt,
            "operation_final_status": "TIMEOUT",
            "operation_error": (
                f"Operation '{operation_name}' did not reach DONE after "
                f"{current_attempt} polls (last status: {op_status})."
            ),
            "lro_completed": False,
        }
        return Event(
            output={
                "route_selected": "failed",
                "operation_name": operation_name,
                "poll_attempt": current_attempt,
                "last_status": op_status,
            },
            route="failed",
            state=state_delta,
        )

    # Operation is still PENDING or RUNNING -> wait and loop back to poll_operation_agent
    await asyncio.sleep(max(1, int(poll_interval_seconds)))
    state_delta = {
        "poll_attempt": current_attempt,
        "operation_progress": progress,
    }
    return Event(
        output=(
            f"Operation '{operation_name}' is still {op_status} (progress={progress}%). "
            f"Proceeding to poll attempt {current_attempt + 1}."
        ),
        route="continue",
        state=state_delta,
    )


# ---------------------------------------------------------------------------
# Node 6: Verify Target Resource State (LlmAgent, route="done")
# ---------------------------------------------------------------------------
verify_resource_state_agent = LlmAgent(
    name="verify_resource_state_agent",
    model=os.environ.get("ADK_MODEL", "gemini-3-flash-preview"),
    include_contents="none",
    description="Verifies that the Compute Engine VM reached its expected terminal state after LRO completion.",
    instruction="""You are the Resource Verification Node in an ADK Workflow.

Workflow Context:
- Target Project: {target_project}
- Target Zone: {default_zone}
- Target VM Name: {target_vm_name}
- Desired Action: {desired_action}
- Expected VM Status: {expected_vm_status}
- Completed Operation Name: {operation_name}
- Poll Attempts Taken: {poll_attempt}

Task:
1. Call `run_gcloud_command` once with:
   - `project`: "projects/{target_project}"
   - `command`: "gcloud compute instances describe {target_vm_name} --zone={default_zone} --project={target_project} --format=\"json(name,status,lastStartTimestamp,lastStopTimestamp)\" --quiet"
2. Output ONLY a valid JSON object with the following fields:
   - "vm_name": string
   - "actual_vm_status": string (exact status returned by gcloud, e.g. "RUNNING" or "TERMINATED")
   - "expected_vm_status": "{expected_vm_status}"
   - "verification_passed": boolean (true if actual_vm_status == "{expected_vm_status}", else false)
   - "operation_name": "{operation_name}"
   - "poll_attempts": {poll_attempt}
   - "last_start_timestamp": string or null
   - "last_stop_timestamp": string or null
   - "summary": string (1-sentence confirmation of the verified operation and VM state)
   Do not wrap the JSON in conversational prose.
""",
    tools=[gcloud_mcp_tools],
    output_key="resource_verification_result",
)


# ---------------------------------------------------------------------------
# Node 7: Handle LRO Failure / Timeout (FunctionNode, route="failed")
# ---------------------------------------------------------------------------
def handle_lro_failure(
    node_input: Any,
    operation_name: str = "",
    target_vm_name: str = DEFAULT_VM_NAME,
    expected_vm_status: str = "RUNNING",
    poll_attempt: int = 0,
    operation_error: Any = None,
) -> Event:
    """Produces a structured failure report if the LRO fails or times out."""
    failure_report = {
        "vm_name": target_vm_name,
        "actual_vm_status": "UNKNOWN",
        "expected_vm_status": expected_vm_status,
        "verification_passed": False,
        "operation_name": operation_name,
        "poll_attempts": poll_attempt,
        "error": operation_error,
        "summary": f"Long-running operation '{operation_name}' failed or timed out after {poll_attempt} poll(s).",
    }
    return Event(
        output=failure_report,
        state={"resource_verification_result": json.dumps(failure_report, indent=2)},
    )


# ---------------------------------------------------------------------------
# Root Workflow Graph Definition (with Conditional Polling Cycle)
# ---------------------------------------------------------------------------
root_agent = Workflow(
    name="gcloud_async_lro_verification_workflow",
    description=(
        "7-node ADK Workflow that submits a long-running gcloud command with --async, "
        "polls the Google Cloud Operation in a conditional loop until DONE, and "
        "verifies the final Compute Engine VM state."
    ),
    edges=[
        (
            "START",
            init_lro_context,
            submit_async_gcloud_agent,
            extract_operation_metadata,
            poll_operation_agent,
            evaluate_poll_status,
            {
                "continue": poll_operation_agent,
                "done": verify_resource_state_agent,
                "failed": handle_lro_failure,
            },
        ),
    ],
)
