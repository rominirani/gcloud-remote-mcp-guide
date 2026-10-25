# Example 07 (Part 4): Handling Long-Running `gcloud` Commands with Remote MCP

When an AI agent invokes a synchronous `gcloud` command against the **Google Cloud CLI Remote MCP Server** (`https://cloudcli.googleapis.com/mcp`)—such as starting/stopping a Compute Engine VM, creating a GKE cluster, or provisioning a Cloud SQL instance—blocking on the HTTP tool call for minutes risks hitting **MCP streamable-HTTP timeouts**, **model turn timeouts**, or leaving the agent with an **ambiguous resource state** if the network drops mid-operation.

This directory provides two complete, live-tested implementations of the **3-Phase Long-Running Operation (LRO) & Verification Pattern**:

1. **Phase 1 — Non-Blocking Submission (`--async`)**: Append `--async --format=json --quiet` to the mutating `gcloud` command so the Remote MCP Server returns the Google Cloud Operation object (`name`, `operationType`, `status: RUNNING`, `targetLink`) in ~1–2 seconds.
2. **Phase 2 — Operation Polling Loop**: Poll `gcloud compute operations describe <operation_name> --zone=<zone> --project=<project> --format=json --quiet` until `status == "DONE"` (or catch any `error.errors` payload).
3. **Phase 3 — Post-Operation Resource Verification**: Because `status == "DONE"` on an operation only proves the control-plane operation finished, query the target resource directly (`gcloud compute instances describe <vm_name>`) and assert that its actual state matches the expected target state (`RUNNING` after `start`, `TERMINATED` after `stop`).

---

## Architecture Comparison

| Dimension | Approach 1: Google ADK `Workflow` Graph ([`app/agent.py`](app/agent.py)) | Approach 2: Gemini Enterprise Managed Agent ([`run_managed_lro.py`](run_managed_lro.py)) |
| :--- | :--- | :--- |
| **Execution Engine** | Client-side / Cloud Run / Agent Engine via `google-adk` (`2.13.0+`) | Server-managed via Gemini Enterprise Agent Platform + Interactions API (`google-genai >= 1.16.0`) |
| **Polling Loop Control** | **Deterministic Python Router Node** (`evaluate_poll_status`) with cyclic graph edges (`"continue"` $\rightarrow$ `poll_operation_agent`) and `asyncio.sleep(poll_interval)` | **Two-Layer Async**: Outer Python loop polls `client.interactions.get(id)` (`background=True`), while inner Server-Side Managed Agent polls `gcloud compute operations describe` |
| **Verification Guarantee** | Explicit `verify_resource_state_agent` (`LlmAgent`) + `LROVerificationResult` Pydantic schema (`verification_passed: bool`) | Structured JSON summary verified by the Python client (`verification_passed == True` and `actual_vm_status == expected_vm_status`) |

---

## Prerequisites

1. **Install `uv`** (if not already installed) and authenticate local Application Default Credentials:
   ```bash
   gcloud auth login
   gcloud auth application-default login
   ```
2. **Enable the Cloud CLI Remote MCP Server** in your Google Cloud project:
   ```bash
   gcloud beta services mcp enable cloudcli.googleapis.com --project=your-project-id
   ```
3. **Set your environment variables**:
   ```bash
   export GOOGLE_CLOUD_PROJECT="your-project-id"
   export GOOGLE_CLOUD_LOCATION="global"
   export GEMINI_API_KEY="your-gemini-api-key" # Required for local ADK execution
   ```

---

## Approach 1: Google ADK 7-Node `Workflow` with Cyclic Polling

[`app/agent.py`](app/agent.py) defines a 7-node ADK `Workflow` using `google-adk`'s dictionary route syntax to create a deterministic polling loop:

```text
START
  │
  ▼
[1. init_lro_context] (FunctionNode)
  │
  ▼
[2. submit_async_gcloud_agent] (LlmAgent -> `--async`)
  │
  ▼
[3. extract_operation_metadata] (FunctionNode)
  │
  ▼
[4. poll_operation_agent] (LlmAgent -> `compute operations describe`) ◄──┐
  │                                                                      │
  ▼                                                                      │
[5. evaluate_poll_status] (Router FunctionNode + `asyncio.sleep`)        │
  ├── route: "continue" (status != DONE) ────────────────────────────────┘
  ├── route: "done"     (status == DONE) ──► [6. verify_resource_state_agent]
  └── route: "failed"   (error / timeout) ─► [7. handle_lro_failure]
```

### Run the ADK LRO Workflow

```bash
cd examples/07-long-running-commands

# Start a VM asynchronously, poll the operation until DONE, and verify RUNNING state
uv run run_adk_lro_workflow.py \
  --project=your-project-id \
  --zone=us-central1-a \
  --instance=demo-web-server \
  --action=start \
  --poll-interval=3 \
  --max-polls=20
```

You can also inspect and run the workflow visually in the ADK Web UI:

```bash
uv run adk web
```

---

## Approach 2: Gemini Enterprise Managed Agent + Interactions API (`background=True`)

[`run_managed_lro.py`](run_managed_lro.py) provisions (or reuses) a server-managed agent (`cloud-cli-lro-agent`) on the Gemini Enterprise Agent Platform (`v1alpha`) wired to `https://cloudcli.googleapis.com/mcp`, and executes it asynchronously via the **Gemini Interactions API** (`client.interactions.create(..., background=True)`).

### Run the Managed Agent LRO Script

```bash
cd examples/07-long-running-commands

# Stop a VM asynchronously via the Managed Agent, poll the interaction, and verify TERMINATED state
uv run run_managed_lro.py \
  --project=your-project-id \
  --zone=us-central1-a \
  --instance=demo-web-server \
  --action=stop \
  --poll-interval=4
```
