# 05 — ADK `Workflow` Graph for Cloud Audit & Live Remediation (`google-adk`)

This example demonstrates a 4-node **ADK `Workflow` graph** (`from google.adk.workflow import Workflow`) that combines deterministic Python functions (`FunctionNode`) with LLM agents (`LlmAgent`) wired to the **Google Cloud CLI Remote MCP Server** (`https://cloudcli.googleapis.com/mcp`).

Instead of stopping at a passive text summary, the workflow **discovers live Compute Engine resources, evaluates state in Python, routes conditionally via `Event(route=...)`, and executes real remediation actions in Google Cloud**—applying compliance labels (`audit-status=needs-remediation,risk-level=high`) to both the stopped VM instance and its attached persistent boot disk (which continues incurring storage charges even while a VM is `TERMINATED`).

---

## Workflow Graph Architecture

```text
START
  │  (node_input: user prompt)
  ▼
[Node 1] init_audit_context (FunctionNode)
  │  Emits Event(output=..., state={target_project, default_region, default_zone, audit_request})
  ▼
[Node 2] vm_discovery_agent (LlmAgent + McpToolset)
  │  Reads {target_project}, {default_zone} from state
  │  Calls run_gcloud_command: gcloud compute instances list --zones={default_zone} --format=json
  │  Saves JSON inventory to state["raw_vm_telemetry"] via output_key="raw_vm_telemetry"
  ▼
[Node 3] evaluate_and_route (FunctionNode)
  │  Reads node_input & state parameters (target_project, default_zone, raw_vm_telemetry)
  │  Emits Event(output=..., route="needs_remediation" | "healthy", state={
  │    target_vm_name, target_vm_status, audit_status, risk_level, remediation_reason
  │  })
  ├──► route == "needs_remediation" ──► [Node 4A] remediation_executor_agent (LlmAgent + McpToolset)
  │                                       1. gcloud compute instances add-labels {target_vm_name}
  │                                          --labels=audit-status=needs-remediation,risk-level=high
  │                                       2. gcloud compute disks add-labels {target_vm_name}
  │                                          --labels=audit-status=needs-remediation,risk-level=high
  │
  └──► route == "healthy"           ──► [Node 4B] healthy_compliance_agent (LlmAgent + McpToolset)
                                          1. gcloud compute instances add-labels {target_vm_name}
                                             --labels=audit-status=compliant,risk-level=low
                                          2. gcloud compute disks add-labels {target_vm_name}
                                             --labels=audit-status=compliant,risk-level=low
```

---

## Files

- `pyproject.toml` — `uv` project configuration (`google-adk[mcp,gcp]>=2.0.0`, `google-auth>=2.35.0`, `pydantic>=2.9.0`).
- `app/agent.py` — Defines `init_audit_context`, `vm_discovery_agent`, `evaluate_and_route`, `remediation_executor_agent`, `healthy_compliance_agent`, and `root_agent = Workflow(...)`.
- `run_workflow.py` — Executes the workflow and streams each node's `Event.output`, `route`, `state_delta`, MCP tool calls, and final `session.state`.

---

## 1. Run the Workflow

```bash
export GOOGLE_CLOUD_PROJECT="your-project-id"
uv run examples/05-adk-workflow-remediation/run_workflow.py \
  --project="${GOOGLE_CLOUD_PROJECT}"
```

---

## 2. Verify the Live Remediation in Google Cloud

After the workflow finishes, verify that both remediation actions actually took effect in your Google Cloud project:

### Check 1: Verify Compliance Labels on the Compute Engine VM
```bash
gcloud compute instances describe demo-web-server \
  --zone=us-central1-a \
  --project="${GOOGLE_CLOUD_PROJECT}" \
  --format="json(name,status,labels)"
```

### Check 2: Verify FinOps & Compliance Labels on the Attached Persistent Boot Disk
```bash
gcloud compute disks describe demo-web-server \
  --zone=us-central1-a \
  --project="${GOOGLE_CLOUD_PROJECT}" \
  --format="json(name,sizeGb,status,labels)"
```
