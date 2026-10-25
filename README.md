# Google Cloud CLI Remote MCP Server (`cloudcli.googleapis.com/mcp`) — Examples

Ready-to-run examples for integrating Google Cloud's stateless **`gcloud` and `bq` Remote MCP Server** (`https://cloudcli.googleapis.com/mcp`) across raw HTTP/`curl`, Local IDEs (Antigravity & Gemini CLI), the **Google Agent Development Kit (ADK)** using **`gemini-3-flash-preview`**, and **Gemini Enterprise Agent Platform Managed Agents**.

---

## Repository Structure

```text
.
├── README.md                                        # Quickstart guide
└── examples/
    ├── 01-wire-protocol-smoke-test/                 # Direct HTTP / JSON-RPC 2.0 smoke test
    │   ├── README.md
    │   ├── pyproject.toml                           # uv project & dependencies
    │   ├── curl_examples.sh                         # Raw curl requests for tools/list & tools/call
    │   └── mcp_probe.py                             # Simple Python smoke test script
    ├── 02-local-ide-config/                         # Local IDE setup (Antigravity / Gemini CLI)
    │   ├── README.md
    │   └── mcp_config.json                          # google_credentials ADC config
    ├── 03-adk-cloud-ops-agent/                      # Google ADK Agent (gemini-3-flash-preview)
    │   ├── README.md
    │   ├── Dockerfile                               # Lean python:3.12-slim + uv container (no Cloud SDK)
    │   ├── pyproject.toml                           # uv project & dependencies
    │   ├── server.py                                # FastAPI ADK entrypoint for Cloud Run
    │   ├── deploy.sh                                # Cloud Run deployment script
    │   ├── run_live_adk_test.py                     # End-to-end ADK test script
    │   ├── test_cloud_run.py                        # Remote Cloud Run verification script
    │   └── app/
    │       ├── __init__.py
    │       └── agent.py                             # McpToolset + dynamic ADC header_provider
    ├── 04-managed-agent/                            # Gemini Enterprise Agent Platform Managed Agents
    │   ├── README.md
    │   ├── pyproject.toml                           # uv project & dependencies
    │   └── manage_agent.py                          # Agent Registry onboarding + Control/Data Plane script
    ├── 05-adk-workflow-remediation/                 # Part 2: ADK Workflow Graph for Cloud Audit & Live Remediation
    │   ├── README.md
    │   ├── pyproject.toml                           # uv project & dependencies
    │   ├── run_workflow.py                          # Executes the 4-node ADK Workflow & prints node events/state
    │   └── app/
    │       ├── __init__.py
    │       └── agent.py                             # FunctionNode + LlmAgent + Event routing + Remote MCP
    ├── 06-adk-workflow-hitl-cloud-run/              # Part 3: Resumable HITL Workflow on Cloud Run + VertexAiSessionService
    │   ├── README.md
    │   ├── pyproject.toml                           # uv project & dependencies
    │   ├── Dockerfile                               # Lean Cloud Run container image
    │   ├── setup_session_engine.py                  # Provisions Vertex AI ReasoningEngine session store
    │   ├── server.py                                # FastAPI server wired to agentengine:// session URI
    │   ├── deploy.sh                                # Cloud Run deployment script with least-privilege SA
    │   ├── run_hitl_client.py                       # 2-request HTTP client (Pause -> Approve/Reject -> Resume)
    │   └── app/
    │       ├── __init__.py
    │       └── agent.py                             # 6-node Resumable ADK Workflow (@node(rerun_on_resume=True) + RequestInput)
    └── 07-long-running-commands/                    # Part 4: Handling & Verifying Long-Running gcloud Commands (--async)
        ├── README.md
        ├── pyproject.toml                           # uv project & dependencies
        ├── run_adk_lro_workflow.py                  # Approach 1: 7-Node ADK Workflow with cyclic LRO polling
        ├── run_managed_lro.py                       # Approach 2: Managed Agent + Stateful Interactions API LRO runner
        └── app/
            ├── __init__.py
            └── agent.py                             # 7-node cyclic ADK Workflow (submit -> poll <-> evaluate -> verify)
```

---

## Prerequisites

1. Set your Google Cloud project ID and enable the Cloud CLI Execution API:
   ```bash
   export GOOGLE_CLOUD_PROJECT="your-project-id"
   gcloud services enable cloudcli.googleapis.com --project="${GOOGLE_CLOUD_PROJECT}" --quiet
   ```
2. Grant `roles/mcp.toolUser` (`mcp.tools.call` permission) and least-privilege IAM roles (such as `roles/viewer`) to your user identity or Service Account:
   ```bash
   gcloud projects add-iam-policy-binding "${GOOGLE_CLOUD_PROJECT}" \
     --member="user:YOUR_EMAIL@example.com" \
     --role="roles/mcp.toolUser" \
     --quiet
   ```
3. Authenticate local Application Default Credentials (ADC) and ensure [`uv`](https://docs.astral.sh/uv/) is installed:
   ```bash
   gcloud auth application-default login
   ```

---

## Quickstart Commands

All Python examples use [`uv`](https://docs.astral.sh/uv/) (`pyproject.toml` + PEP 723 inline script metadata) so Python `>=3.11` and dependencies are resolved automatically without manual virtual environment setup.

### 1. Run the `curl` or Python Smoke Test
```bash
export GOOGLE_CLOUD_PROJECT="your-project-id"
bash examples/01-wire-protocol-smoke-test/curl_examples.sh
# or
uv run examples/01-wire-protocol-smoke-test/mcp_probe.py --project="${GOOGLE_CLOUD_PROJECT}"
```

### 2. Run the ADK Agent (`gemini-3-flash-preview`)
```bash
cd examples/03-adk-cloud-ops-agent
uv sync
export GOOGLE_CLOUD_PROJECT="your-project-id"
export GOOGLE_CLOUD_LOCATION="global"
export GOOGLE_GENAI_USE_VERTEXAI="True"
uv run adk web
```

### 3. Preview, Provision, Verify, or Interact with a Managed Agent
```bash
export GOOGLE_CLOUD_PROJECT="your-project-id"
# Preview payloads (--dry-run)
uv run examples/04-managed-agent/manage_agent.py --project="${GOOGLE_CLOUD_PROJECT}" --dry-run

# Provision in Google Cloud
uv run examples/04-managed-agent/manage_agent.py --project="${GOOGLE_CLOUD_PROJECT}" --action=provision

# Check provisioned status (Control Plane + Agent Registry)
uv run examples/04-managed-agent/manage_agent.py --project="${GOOGLE_CLOUD_PROJECT}" --action=status

# Interact via the Data Plane (Interactions API)
uv run examples/04-managed-agent/manage_agent.py --project="${GOOGLE_CLOUD_PROJECT}" --action=interact
```

### 4. Run the Part 2 ADK `Workflow` Graph (Cloud Audit & Live Remediation)
```bash
export GOOGLE_CLOUD_PROJECT="your-project-id"
uv run examples/05-adk-workflow-remediation/run_workflow.py --project="${GOOGLE_CLOUD_PROJECT}"
```

### 5. Run the Part 3 Resumable HITL Workflow (Cloud Run & `VertexAiSessionService`)
```bash
export GOOGLE_CLOUD_PROJECT="your-project-id"

# Provision/reuse the Vertex AI Agent Engine session store
uv run --project examples/06-adk-workflow-hitl-cloud-run \
  examples/06-adk-workflow-hitl-cloud-run/setup_session_engine.py \
  --project="${GOOGLE_CLOUD_PROJECT}"

# Run the 2-request HITL client (reject or approve)
uv run --project examples/06-adk-workflow-hitl-cloud-run \
  examples/06-adk-workflow-hitl-cloud-run/run_hitl_client.py \
  --url="http://127.0.0.1:8080" \
  --decision=approve
```

### 6. Run the Part 4 Long-Running `gcloud` Commands (`--async`) with Polling & Resource Verification
```bash
export GOOGLE_CLOUD_PROJECT="your-project-id"

# Approach 1: 7-Node ADK Workflow with Cyclic Operation Polling & Resource Verification
uv run --project examples/07-long-running-commands \
  examples/07-long-running-commands/run_adk_lro_workflow.py \
  --project="${GOOGLE_CLOUD_PROJECT}" \
  --action=start

# Approach 2: Managed Agent + Stateful Interactions API LRO & Verification
uv run --project examples/07-long-running-commands \
  examples/07-long-running-commands/run_managed_lro.py \
  --project="${GOOGLE_CLOUD_PROJECT}" \
  --action=stop
```
