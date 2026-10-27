"""Part 3: 6-Node Resumable ADK Workflow with Human-in-the-Loop (HITL) Approval Gate.

Designed for stateless Cloud Run deployment backed by `VertexAiSessionService`
(`agentengine://projects/{project}/locations/{region}/reasoningEngines/{id}`)
and the Google Cloud CLI Remote MCP Server (`https://cloudcli.googleapis.com/mcp`):

  1. `init_audit_context` (`FunctionNode`):
     Seeds `target_project`, `default_region`, `default_zone`, and `audit_request`.

  2. `vm_discovery_agent` (`LlmAgent` + `McpToolset`):
     Calls `run_gcloud_command` (`gcloud compute instances list`) via Remote MCP
     and stores the JSON inventory in `state["raw_vm_telemetry"]`.

  3. `evaluate_and_route` (`FunctionNode`):
     Deterministically inspects `raw_vm_telemetry` and routes to:
       - `route="healthy"`           -> `healthy_compliance_agent` (Node 4A)
       - `route="needs_remediation"` -> `request_remediation_approval` (Node 4B)

  4A. `healthy_compliance_agent` (`LlmAgent` + `McpToolset`, route="healthy"):
      Applies `audit-status=compliant,risk-level=low` labels to the VM and boot
      disk and finishes in a single HTTP request (no HITL pause).

  4B. `request_remediation_approval` (`@node(rerun_on_resume=True)` HITL Gate):
      - Pass 1 (HTTP Request 1): When `ctx.resume_inputs.get("approve_vm_remediation")`
        is `None`, yields `RequestInput(interrupt_id="approve_vm_remediation", ...)`.
        ADK emits an `adk_request_input` tool call event, persists the checkpoint to
        `VertexAiSessionService`, and closes HTTP Request 1 (`200 OK`).
      - Pass 2 (HTTP Request 2): When the human operator submits a `functionResponse`
        for `id="approve_vm_remediation"`, ADK fast-forwards past Nodes 1–3 without
        re-running them, re-enters `request_remediation_approval`, and routes to:
          * `route="approved"` -> `remediation_executor_agent` (Node 5A)
          * `route="rejected"` -> `remediation_rejected_node` (Node 5B)

  5A. `remediation_executor_agent` (`LlmAgent` + `McpToolset`, route="approved"):
      Tags the VM and boot disk with `audit-status=remediated,risk-level=medium`
      and starts the stopped VM (`gcloud compute instances start --async`).

  5B. `remediation_rejected_node` (`FunctionNode`, route="rejected"):
      Records the operator's rejection in session state and leaves the VM `TERMINATED`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.models import Gemini
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)
from google.adk.workflow import Workflow, node
from google.genai import types

DEFAULT_PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "YOUR_PROJECT_ID")
DEFAULT_REGION = os.environ.get("GOOGLE_CLOUD_REGION", "us-central1")
DEFAULT_ZONE = os.environ.get("GOOGLE_CLOUD_ZONE", "us-central1-a")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", DEFAULT_REGION)
GCLOUD_MCP_URL = "https://cloudcli.googleapis.com/mcp"
HITL_INTERRUPT_ID = "approve_vm_remediation"
DEFAULT_APPROVAL_TTL_SECONDS = int(
    os.environ.get("HITL_APPROVAL_TTL_SECONDS", "900")
)
DEFAULT_TIMEOUT_ACTION = os.environ.get(
    "HITL_DEFAULT_TIMEOUT_ACTION", "reject"
)


def _get_gemini_model() -> Gemini:
    """Returns the Gemini model instance configured for Vertex AI."""
    return Gemini(
        model=os.environ.get("ADK_MODEL", "gemini-3.8-flash"),
        client_kwargs={"location": "global"},
    )


def get_dynamic_mcp_headers(ctx: Any = None) -> dict[str, str]:
    """Refreshes Application Default Credentials (ADC) on each MCP tool call.

    On Cloud Run, a static Bearer token minted at import time expires after 60 minutes.
    Using `header_provider=get_dynamic_mcp_headers` ensures every MCP call uses a
    fresh OAuth token from the Cloud Run service account or local ADC.
    """
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
    connection_params=StreamableHTTPConnectionParams(url=GCLOUD_MCP_URL),
    header_provider=get_dynamic_mcp_headers,
    tool_filter=["run_gcloud_command", "run_bq_command"],
)


# ---------------------------------------------------------------------------
# Node 1: Context Initialization (FunctionNode)
# ---------------------------------------------------------------------------
def init_audit_context(node_input: str) -> Event:
    """Initializes workflow state variables from the user prompt and environment."""
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", DEFAULT_PROJECT_ID)
    region = os.environ.get("GOOGLE_CLOUD_REGION", DEFAULT_REGION)
    zone = os.environ.get("GOOGLE_CLOUD_ZONE", DEFAULT_ZONE)

    state_init = {
        "target_project": project_id,
        "default_region": region,
        "default_zone": zone,
        "audit_request": str(node_input).strip(),
    }
    return Event(
        output={
            "message": f"Initialized HITL audit context for project '{project_id}' in zone '{zone}'.",
            **state_init,
        },
        state=state_init,
    )


# ---------------------------------------------------------------------------
# Node 2: Cloud Discovery via Remote MCP (LlmAgent)
# ---------------------------------------------------------------------------
vm_discovery_agent = LlmAgent(
    name="vm_discovery_agent",
    model=_get_gemini_model(),
    description="Discovers Compute Engine VM instances in the target project and zone via Remote MCP.",
    instruction="""You are the Cloud Discovery Node in an automated ADK Workflow.

Workflow State Variables:
- Target Project: {target_project}
- Default Zone: {default_zone}
- Audit Request: {audit_request}

Task:
1. Call `run_gcloud_command` with:
   - `project`: "projects/{target_project}"
   - `command`: "gcloud compute instances list --zones={default_zone} --project={target_project} --format=json"
2. Output ONLY a valid JSON array summarizing the discovered VM instances, where each object contains:
   - "name" (string)
   - "status" (string, e.g. "RUNNING" or "TERMINATED")
   - "zone" (string, short zone name e.g. "us-central1-a")
   - "machineType" (string, short machine type e.g. "e2-micro")
   Do not wrap the JSON in conversational text.
""",
    tools=[gcloud_mcp_tools],
    output_key="raw_vm_telemetry",
)


# ---------------------------------------------------------------------------
# Node 3: Deterministic Evaluation & Conditional Routing (FunctionNode)
# ---------------------------------------------------------------------------
def _extract_vm_list(raw_text: str) -> list[dict[str, Any]]:
    """Extracts a JSON list of VM dicts from the discovery agent's output."""
    cleaned = str(raw_text or "").strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        if isinstance(parsed, dict):
            return [parsed]
    except json.JSONDecodeError:
        pass

    bracket_match = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if bracket_match:
        try:
            parsed = json.loads(bracket_match.group(0))
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        except json.JSONDecodeError:
            pass
    return []


def evaluate_and_route(
    node_input: str,
    target_project: str,
    default_zone: str,
    raw_vm_telemetry: str,
) -> Event:
    """Evaluates discovered VM telemetry and routes to 'needs_remediation' or 'healthy'."""
    telemetry_source = raw_vm_telemetry or str(node_input)
    vms = _extract_vm_list(telemetry_source)

    non_running_vms = [
        vm for vm in vms if str(vm.get("status", "")).upper() != "RUNNING"
    ]

    if non_running_vms:
        flagged_vm = non_running_vms[0]
        vm_name = str(flagged_vm.get("name", "unknown-vm"))
        vm_status = str(flagged_vm.get("status", "UNKNOWN"))
        vm_zone = str(flagged_vm.get("zone", default_zone)).split("/")[-1]
        route = "needs_remediation"
        audit_status = "needs-remediation"
        risk_level = "high"
        reason = (
            f"VM '{vm_name}' in zone '{vm_zone}' is in '{vm_status}' state "
            f"(expected RUNNING). Human approval required before starting VM."
        )
    elif vms:
        first_vm = vms[0]
        vm_name = str(first_vm.get("name", "unknown-vm"))
        vm_status = str(first_vm.get("status", "RUNNING"))
        vm_zone = str(first_vm.get("zone", default_zone)).split("/")[-1]
        route = "healthy"
        audit_status = "compliant"
        risk_level = "low"
        reason = f"All {len(vms)} VM(s) in zone '{vm_zone}' are RUNNING."
    else:
        vm_name = "none"
        vm_status = "NO_VMS_FOUND"
        vm_zone = default_zone
        route = "healthy"
        audit_status = "compliant"
        risk_level = "low"
        reason = f"No Compute Engine VMs found in zone '{default_zone}'."

    ttl_seconds = int(
        os.environ.get(
            "HITL_APPROVAL_TTL_SECONDS", str(DEFAULT_APPROVAL_TTL_SECONDS)
        )
    )
    default_timeout_action = os.environ.get(
        "HITL_DEFAULT_TIMEOUT_ACTION", DEFAULT_TIMEOUT_ACTION
    ).lower()
    requested_at = round(time.time(), 3)
    expires_at = round(requested_at + ttl_seconds, 3)
    expires_at_iso = datetime.fromtimestamp(
        expires_at, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    exact_gcloud_commands = (
        [
            f"gcloud compute instances add-labels {vm_name} --zone={vm_zone} --labels=audit-status=remediated,risk-level=low --project={target_project} --quiet",
            f"gcloud compute disks add-labels {vm_name} --zone={vm_zone} --labels=audit-status=remediated,risk-level=low --project={target_project} --quiet",
            f"gcloud compute instances start {vm_name} --zone={vm_zone} --project={target_project} --async --format=json --quiet",
        ]
        if route == "needs_remediation"
        else []
    )
    action_digest = (
        hashlib.sha256(
            json.dumps(exact_gcloud_commands).encode("utf-8")
        ).hexdigest()[:12]
        if exact_gcloud_commands
        else ""
    )

    state_updates = {
        "target_vm_name": vm_name,
        "target_vm_status": vm_status,
        "default_zone": vm_zone,
        "audit_status": audit_status,
        "risk_level": risk_level,
        "remediation_reason": reason,
        "terminated_vms": non_running_vms,
        "exact_gcloud_commands": exact_gcloud_commands,
        "action_digest": action_digest,
        "approval_requested_at": requested_at,
        "approval_expires_at": expires_at,
        "approval_expires_at_iso": expires_at_iso,
        "approval_ttl_seconds": ttl_seconds,
        "default_timeout_action": default_timeout_action,
    }

    return Event(
        output={
            "route_selected": route,
            **state_updates,
        },
        route=route,
        state=state_updates,
    )


# ---------------------------------------------------------------------------
# Node 4A: Healthy Compliance Sign-Off (LlmAgent, route="healthy")
# ---------------------------------------------------------------------------
healthy_compliance_agent = LlmAgent(
    name="healthy_compliance_agent",
    model=_get_gemini_model(),
    description="Tags healthy VMs and their boot disks as compliant in a single HTTP request.",
    instruction="""You are the Healthy Compliance Node in an ADK Workflow.
All inspected Compute Engine VMs in zone {default_zone} passed the health check (status=RUNNING), so no Human-in-the-Loop approval was needed.

Workflow State Variables:
- Target Project: {target_project}
- Target Zone: {default_zone}
- Target VM Name: {target_vm_name}
- Audit Status Label: {audit_status}
- Risk Level Label: {risk_level}

Task:
1. If `{target_vm_name}` is not "none", call `run_gcloud_command` to label the VM instance:
   - `project`: "projects/{target_project}"
   - `command`: "gcloud compute instances add-labels {target_vm_name} --zone={default_zone} --labels=audit-status={audit_status},risk-level={risk_level} --project={target_project} --quiet"
2. If `{target_vm_name}` is not "none", call `run_gcloud_command` to label the VM's boot disk:
   - `project`: "projects/{target_project}"
   - `command`: "gcloud compute disks add-labels {target_vm_name} --zone={default_zone} --labels=audit-status={audit_status},risk-level={risk_level} --project={target_project} --quiet"
3. Summarize the single-request compliance sign-off actions completed.
""",
    tools=[gcloud_mcp_tools],
    output_key="workflow_execution_result",
)


# ---------------------------------------------------------------------------
# Node 4B: Human-in-the-Loop (HITL) Approval Gate (@node(rerun_on_resume=True))
# ---------------------------------------------------------------------------
@node(rerun_on_resume=True)
async def request_remediation_approval(
    ctx: Context, node_input: dict[str, Any]
) -> AsyncGenerator[Event | RequestInput, None]:
    """Pauses the workflow on Pass 1 via RequestInput; enforces TTL expiry and default timeout actions on Pass 2."""
    resume_data = ctx.resume_inputs.get(HITL_INTERRUPT_ID)

    # Pass 1 (HTTP Request 1): Emit RequestInput interrupt and checkpoint to VertexAiSessionService
    if resume_data is None:
        vm_name = ctx.state.get("target_vm_name", "unknown-vm")
        vm_zone = ctx.state.get("default_zone", DEFAULT_ZONE)
        project_id = ctx.state.get("target_project", DEFAULT_PROJECT_ID)
        reason = ctx.state.get("remediation_reason", "")
        ttl_seconds = int(
            ctx.state.get("approval_ttl_seconds", DEFAULT_APPROVAL_TTL_SECONDS)
        )
        expires_at = float(ctx.state.get("approval_expires_at", 0.0) or 0.0)
        expires_at_iso = str(ctx.state.get("approval_expires_at_iso", ""))
        default_timeout_action = str(
            ctx.state.get("default_timeout_action", DEFAULT_TIMEOUT_ACTION)
        )
        exact_gcloud_commands = list(
            ctx.state.get("exact_gcloud_commands") or []
        )
        action_digest = str(ctx.state.get("action_digest", ""))

        proposed_actions = [
            f"Label VM '{vm_name}' with audit-status=remediated,risk-level=low",
            f"Label persistent boot disk '{vm_name}' with audit-status=remediated,risk-level=low",
            f"Start stopped VM '{vm_name}' in zone '{vm_zone}' (`gcloud compute instances start --async`)",
        ]
        prompt_msg = (
            f"HITL Approval Required: {reason} "
            f"Approve live remediation on '{vm_name}' in project '{project_id}'? "
            f"(Action Digest={action_digest}, Expires at {expires_at_iso} [TTL={ttl_seconds}s], "
            f"default action on timeout='{default_timeout_action}')"
        )

        yield RequestInput(
            interrupt_id=HITL_INTERRUPT_ID,
            message=prompt_msg,
            payload={
                "interrupt_id": HITL_INTERRUPT_ID,
                "target_project": project_id,
                "target_zone": vm_zone,
                "target_vm_name": vm_name,
                "reason": reason,
                "proposed_actions": proposed_actions,
                "exact_gcloud_commands": exact_gcloud_commands,
                "action_digest": action_digest,
                "ttl_seconds": ttl_seconds,
                "expires_at": expires_at,
                "expires_at_iso": expires_at_iso,
                "default_timeout_action": default_timeout_action,
            },
        )
        return

    # Pass 2 (HTTP Request 2): Evaluate human decision or timeout watchdog from ctx.resume_inputs
    if isinstance(resume_data, str):
        try:
            resume_data = json.loads(resume_data)
        except json.JSONDecodeError:
            resume_data = {
                "approved": resume_data.strip().lower()
                in ("true", "yes", "y", "approve", "approved")
            }

    timed_out = False
    submitted_digest = ""
    if isinstance(resume_data, dict):
        timed_out = bool(resume_data.get("timed_out", False))
        approved = bool(resume_data.get("approved", False))
        approver = str(resume_data.get("approver", "sre-operator"))
        comment = str(resume_data.get("reason", resume_data.get("comment", "")))
        submitted_digest = str(resume_data.get("action_digest", "")).strip()
    else:
        approved = bool(resume_data)
        approver = "sre-operator"
        comment = ""

    now_ts = time.time()
    expires_at = float(ctx.state.get("approval_expires_at", 0.0) or 0.0)
    expires_at_iso = str(ctx.state.get("approval_expires_at_iso", ""))
    ttl_seconds = int(
        ctx.state.get("approval_ttl_seconds", DEFAULT_APPROVAL_TTL_SECONDS)
    )
    default_timeout_action = str(
        ctx.state.get("default_timeout_action", DEFAULT_TIMEOUT_ACTION)
    ).lower()
    expected_digest = str(ctx.state.get("action_digest", "")).strip()
    is_expired = expires_at > 0 and now_ts > expires_at
    is_digest_mismatch = bool(
        submitted_digest
        and expected_digest
        and submitted_digest != expected_digest
    )

    if timed_out:
        # Feature 2: Default Action when Nobody Answers within TTL
        approved = default_timeout_action == "approve"
        route = "approved" if approved else "rejected"
        approval_status = f"timed_out_default_{route}"
        approver = (
            approver if approver != "sre-operator" else "system-ttl-watchdog"
        )
        comment = comment or (
            f"No human response received within {ttl_seconds}s TTL "
            f"(expired at {expires_at_iso}). Executed default_timeout_action='{default_timeout_action}'."
        )
        audit_status = "remediated" if approved else "remediation-timed-out"
        summary_text = (
            f"[Node 4B: request_remediation_approval] No human response within {ttl_seconds}s TTL; "
            f"watchdog '{approver}' applied default_timeout_action='{default_timeout_action}' "
            f"for VM '{ctx.state.get('target_vm_name')}' -> route='{route}'"
        )
    elif is_digest_mismatch:
        # Feature 3: Exact Action Digest Verification (Reject approvals bound to a different command plan)
        approved = False
        route = "rejected"
        approval_status = "digest_mismatch"
        audit_status = "remediation-digest-mismatch"
        comment = (
            f"Action digest mismatch blocked: approval was signed for digest '{submitted_digest}', "
            f"but current command plan digest is '{expected_digest}'."
        )
        summary_text = (
            f"[Node 4B: request_remediation_approval] ACTION DIGEST MISMATCH BLOCKED for VM "
            f"'{ctx.state.get('target_vm_name')}': submitted={submitted_digest!r} != expected={expected_digest!r} -> route='rejected'"
        )
    elif is_expired:
        # Feature 1: Stale Approval Guard (Reject late approvals after TTL expiry)
        late_by = round(now_ts - expires_at, 1)
        approved = False
        route = "rejected"
        approval_status = "expired"
        audit_status = "remediation-expired"
        comment = (
            f"Stale response blocked: operator '{approver}' responded {late_by}s after "
            f"the {ttl_seconds}s approval window expired at {expires_at_iso}. "
            f"(Original comment: {comment!r})"
        )
        summary_text = (
            f"[Node 4B: request_remediation_approval] STALE APPROVAL BLOCKED for VM "
            f"'{ctx.state.get('target_vm_name')}': response from '{approver}' arrived "
            f"{late_by}s after TTL ({ttl_seconds}s) expired at {expires_at_iso} -> route='rejected'"
        )
    else:
        # Normal timely human decision within TTL
        route = "approved" if approved else "rejected"
        approval_status = route
        audit_status = "remediated" if approved else "remediation-rejected"
        summary_text = (
            f"[Node 4B: request_remediation_approval] Operator '{approver}' "
            f"{route.upper()} remediation for VM '{ctx.state.get('target_vm_name')}' "
            f"(comment: {comment!r}) -> route='{route}'"
        )

    decision_state = {
        "approval_status": approval_status,
        "approved_by": approver,
        "approval_comment": comment,
        "audit_status": audit_status,
        "risk_level": "low" if approved else "high",
    }
    yield Event(
        author="request_remediation_approval",
        content=types.Content(
            role="model", parts=[types.Part.from_text(text=summary_text)]
        ),
        output=decision_state,
        route=route,
        state=decision_state,
    )


# ---------------------------------------------------------------------------
# Node 5A: Approved Remediation Executor (LlmAgent + Remote MCP, route="approved")
# ---------------------------------------------------------------------------
def enforce_approved_commands_callback(
    tool: Any,
    args: dict[str, Any],
    tool_context: Any,
) -> dict[str, Any] | None:
    """OWASP LLM06 Guardrail: Enforces in Python that Node 5A only runs human-approved commands."""
    state = tool_context.state
    if state.get("approval_status") != "approved":
        return {
            "error": (
                "Blocked by before_tool_callback (OWASP LLM06): "
                f"approval_status is '{state.get('approval_status')}', not 'approved'."
            )
        }

    approved_commands: list[str] = list(state.get("exact_gcloud_commands") or [])
    expected_digest = str(state.get("action_digest", "")).strip()
    computed_digest = (
        hashlib.sha256(
            json.dumps(approved_commands).encode("utf-8")
        ).hexdigest()[:12]
        if approved_commands
        else ""
    )
    if not expected_digest or computed_digest != expected_digest:
        return {
            "error": (
                "Blocked by before_tool_callback (OWASP LLM06): "
                f"computed digest '{computed_digest}' does not match approved action_digest '{expected_digest}'."
            )
        }

    target_project = str(state.get("target_project", ""))
    default_zone = str(state.get("default_zone", ""))
    target_vm_name = str(state.get("target_vm_name", ""))
    precondition_check_cmd = (
        f"gcloud compute instances describe {target_vm_name} "
        f"--zone={default_zone} --project={target_project} --format=value(status)"
    )
    allowed_commands = set(approved_commands) | {precondition_check_cmd}

    requested_command = " ".join(str(args.get("command", "")).strip().split())
    normalized_allowed = {
        " ".join(cmd.strip().split()) for cmd in allowed_commands
    }
    if requested_command not in normalized_allowed:
        return {
            "error": (
                "Blocked by before_tool_callback (OWASP LLM06): "
                f"command '{requested_command}' is not in the human-approved exact_gcloud_commands allowlist."
            )
        }

    return None


remediation_executor_agent = LlmAgent(
    name="remediation_executor_agent",
    model=_get_gemini_model(),
    description="Re-verifies live VM preconditions on resume, then executes VM/disk labeling and VM startup.",
    instruction="""You are the Approved Remediation Executor Node in a Resumable ADK Workflow.
A human operator (`{approved_by}`) has explicitly APPROVED remediating the stopped VM `{target_vm_name}` (comment: `{approval_comment}`).

Workflow State Variables:
- Target Project: {target_project}
- Target Zone: {default_zone}
- Target VM Name: {target_vm_name}
- Expected Precondition Status at Pause Time: {target_vm_status}
- Approved By: {approved_by}
- Audit Status Label: {audit_status}
- Risk Level Label: {risk_level}
- Approved Command Allowlist (enforced by Python before_tool_callback): {exact_gcloud_commands}

Step 0 — Precondition Re-Verification on Resume (Guard against state drift while paused):
- First, call `run_gcloud_command` to inspect the live VM status right now:
  - `project`: "projects/{target_project}"
  - `command`: "gcloud compute instances describe {target_vm_name} --zone={default_zone} --project={target_project} --format=value(status)"
- If the live status is already `RUNNING` (meaning the incident resolved or another operator fixed it while the workflow was paused), DO NOT run `gcloud compute instances start`; simply report that precondition re-verification detected the VM is already `RUNNING` and skipped duplicate startup.

If the live status still matches `{target_vm_status}` (`TERMINATED`), execute ALL THREE of the following remediation actions in Google Cloud using `run_gcloud_command`:

Action 1 — Tag the VM instance with compliance labels:
- `project`: "projects/{target_project}"
- `command`: "gcloud compute instances add-labels {target_vm_name} --zone={default_zone} --labels=audit-status={audit_status},risk-level={risk_level} --project={target_project} --quiet"

Action 2 — Tag the VM's persistent boot disk with matching labels:
- `project`: "projects/{target_project}"
- `command`: "gcloud compute disks add-labels {target_vm_name} --zone={default_zone} --labels=audit-status={audit_status},risk-level={risk_level} --project={target_project} --quiet"

Action 3 — Start the stopped VM asynchronously (`--async`) so it returns to service without blocking the HTTP response:
- `project`: "projects/{target_project}"
- `command`: "gcloud compute instances start {target_vm_name} --zone={default_zone} --project={target_project} --async --format=json --quiet"

Always use '=' between flag names and values. After all commands succeed, provide a concise summary including the `operation-...` ID from Action 3.
""",
    tools=[gcloud_mcp_tools],
    before_tool_callback=enforce_approved_commands_callback,
    output_key="workflow_execution_result",
)


# ---------------------------------------------------------------------------
# Node 5B: Rejection / Expiry / Timeout Audit Handler (FunctionNode, route="rejected")
# ---------------------------------------------------------------------------
def remediation_rejected_node(
    target_project: str,
    default_zone: str,
    target_vm_name: str,
    approved_by: str,
    approval_comment: str,
    approval_status: str = "rejected",
) -> Event:
    """Logs that remediation was rejected, expired, or timed out, leaving the VM untouched."""
    status_label = {
        "expired": "REMEDIATION_EXPIRED",
        "timed_out_default_rejected": "REMEDIATION_TIMED_OUT",
    }.get(approval_status, "REMEDIATION_REJECTED")
    report = {
        "status": status_label,
        "approval_status": approval_status,
        "target_project": target_project,
        "target_zone": default_zone,
        "target_vm_name": target_vm_name,
        "rejected_by": approved_by,
        "rejection_reason": approval_comment,
        "action_taken": f"No changes made. VM '{target_vm_name}' remains TERMINATED.",
    }
    text_msg = (
        f"Remediation cancelled [{status_label}] by '{approved_by}' "
        f"(reason: {approval_comment!r}). VM '{target_vm_name}' in zone "
        f"'{default_zone}' was left in TERMINATED state."
    )
    return Event(
        author="remediation_rejected_node",
        content=types.Content(role="model", parts=[types.Part.from_text(text=text_msg)]),
        output=report,
        state={"workflow_execution_result": text_msg},
    )


# ---------------------------------------------------------------------------
# Root 6-Node Resumable Workflow & App Definition
# ---------------------------------------------------------------------------
root_agent = Workflow(
    name="cloud_audit_hitl_remediation_workflow",
    description=(
        "6-node Resumable ADK Workflow with a Human-in-the-Loop (HITL) approval gate "
        "backed by VertexAiSessionService and the Google Cloud CLI Remote MCP Server."
    ),
    edges=[
        (
            "START",
            init_audit_context,
            vm_discovery_agent,
            evaluate_and_route,
            {
                "healthy": healthy_compliance_agent,
                "needs_remediation": request_remediation_approval,
            },
        ),
        (
            request_remediation_approval,
            {
                "approved": remediation_executor_agent,
                "rejected": remediation_rejected_node,
            },
        ),
    ],
)

app = App(
    name="app",
    root_agent=root_agent,
    resumability_config=ResumabilityConfig(is_resumable=True),
)
