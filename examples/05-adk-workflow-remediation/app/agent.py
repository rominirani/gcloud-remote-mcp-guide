"""ADK Workflow Graph for Cloud Audit & Live Remediation via Remote MCP.

Implements a 4-node ADK `Workflow` graph (`from google.adk.workflow import Workflow`)
connected to `https://cloudcli.googleapis.com/mcp`:

  1. `init_audit_context` (`FunctionNode`):
     - Receives the user prompt via `node_input` from `START`.
     - Emits `Event(output=..., state={target_project, default_region, default_zone, audit_request})`.

  2. `vm_discovery_agent` (`LlmAgent` + `McpToolset`):
     - Reads `{target_project}` and `{default_zone}` from workflow state.
     - Calls `run_gcloud_command` to list Compute Engine instances in JSON format.
     - Saves the raw inventory to `state["raw_vm_telemetry"]` via `output_key="raw_vm_telemetry"`.

  3. `evaluate_and_route` (`FunctionNode`):
     - Receives `node_input` from `vm_discovery_agent` and reads `target_project`,
       `default_zone`, and `raw_vm_telemetry` directly as state-bound parameters.
     - Evaluates VM status (`RUNNING` vs `TERMINATED` / stopped).
     - Emits `Event(output=..., route="needs_remediation" | "healthy", state={...})`.

  4A. `remediation_executor_agent` (`LlmAgent` + `McpToolset`, route="needs_remediation"):
      - Reads `{target_project}`, `{default_zone}`, `{target_vm_name}`, `{audit_status}`,
        `{risk_level}`, and `{remediation_reason}` from workflow state.
      - Executes two live remediation actions via `run_gcloud_command`:
        1. Applies compliance labels (`audit-status=needs-remediation,risk-level=high`)
           to the stopped VM (`gcloud compute instances add-labels`).
        2. Applies matching compliance & FinOps labels to the VM's persistent
           boot disk (`gcloud compute disks add-labels`), which continues to
           incur storage charges even while the VM is TERMINATED.

  4B. `healthy_compliance_agent` (`LlmAgent` + `McpToolset`, route="healthy"):
      - Applies `audit-status=compliant,risk-level=low` labels to the VM and
        its persistent boot disk.
"""

from __future__ import annotations

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
DEFAULT_REGION = os.environ.get("GOOGLE_CLOUD_REGION", "us-central1")
DEFAULT_ZONE = os.environ.get("GOOGLE_CLOUD_ZONE", "us-central1-a")
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

    state_Init = {
        "target_project": project_id,
        "default_region": region,
        "default_zone": zone,
        "audit_request": str(node_input).strip(),
    }
    return Event(
        output={
            "message": f"Initialized audit context for project '{project_id}' in zone '{zone}'.",
            **state_Init,
        },
        state=state_Init,
    )


# ---------------------------------------------------------------------------
# Node 2: Cloud Discovery via Remote MCP (LlmAgent)
# ---------------------------------------------------------------------------
vm_discovery_agent = LlmAgent(
    name="vm_discovery_agent",
    model=os.environ.get("ADK_MODEL", "gemini-3-flash-preview"),
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
   - "machineType" (string, short machine type e.g. "e2-medium")
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
    cleaned = raw_text.strip()
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

    # Fallback: locate first [...] block
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
            f"(expected RUNNING)."
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

    state_updates = {
        "target_vm_name": vm_name,
        "target_vm_status": vm_status,
        "default_zone": vm_zone,
        "audit_status": audit_status,
        "risk_level": risk_level,
        "remediation_reason": reason,
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
# Node 4A: Live Remediation Executor (LlmAgent + Remote MCP, route="needs_remediation")
# ---------------------------------------------------------------------------
remediation_executor_agent = LlmAgent(
    name="remediation_executor_agent",
    model=os.environ.get("ADK_MODEL", "gemini-3-flash-preview"),
    description="Executes live VM and persistent boot disk compliance labeling for non-running VMs.",
    instruction="""You are the Automated Remediation Node in an ADK Workflow.
The upstream evaluation node detected a non-compliant Compute Engine VM and routed execution to you.

Workflow State Variables:
- Target Project: {target_project}
- Target Zone: {default_zone}
- Target VM Name: {target_vm_name}
- VM Current Status: {target_vm_status}
- Audit Status Label: {audit_status}
- Risk Level Label: {risk_level}
- Remediation Reason: {remediation_reason}

Execute BOTH of the following remediation actions in Google Cloud using `run_gcloud_command`:

Action 1 — Tag the non-compliant VM instance with compliance labels:
- `project`: "projects/{target_project}"
- `command`: "gcloud compute instances add-labels {target_vm_name} --zone={default_zone} --labels=audit-status={audit_status},risk-level={risk_level} --project={target_project} --quiet"

Action 2 — Tag the VM's attached persistent boot disk (`{target_vm_name}`) with matching FinOps & compliance labels (since persistent disks continue incurring storage cost while a VM is TERMINATED):
- `project`: "projects/{target_project}"
- `command`: "gcloud compute disks add-labels {target_vm_name} --zone={default_zone} --labels=audit-status={audit_status},risk-level={risk_level} --project={target_project} --quiet"

Always use '=' between flag names and values. After both commands succeed, provide a concise summary of the two remediation actions executed in Google Cloud.
""",
    tools=[gcloud_mcp_tools],
    output_key="workflow_execution_result",
)


# ---------------------------------------------------------------------------
# Node 4B: Healthy Compliance Sign-Off (LlmAgent + Remote MCP, route="healthy")
# ---------------------------------------------------------------------------
healthy_compliance_agent = LlmAgent(
    name="healthy_compliance_agent",
    model=os.environ.get("ADK_MODEL", "gemini-3-flash-preview"),
    description="Tags healthy VMs and their boot disks as compliant.",
    instruction="""You are the Healthy Compliance Node in an ADK Workflow.
All inspected Compute Engine VMs in zone {default_zone} passed the health check.

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
3. Summarize the compliance sign-off actions completed.
""",
    tools=[gcloud_mcp_tools],
    output_key="workflow_execution_result",
)


# ---------------------------------------------------------------------------
# Root Workflow Graph Definition
# ---------------------------------------------------------------------------
root_agent = Workflow(
    name="cloud_audit_remediation_workflow",
    description=(
        "4-node ADK Workflow that discovers Compute Engine VMs via the "
        "Google Cloud CLI Remote MCP Server, evaluates state via FunctionNode, "
        "and conditionally executes live VM labeling and Cloud Logging writes."
    ),
    edges=[
        ("START", init_audit_context, vm_discovery_agent, evaluate_and_route),
        (
            evaluate_and_route,
            {
                "needs_remediation": remediation_executor_agent,
                "healthy": healthy_compliance_agent,
            },
        ),
    ],
)
