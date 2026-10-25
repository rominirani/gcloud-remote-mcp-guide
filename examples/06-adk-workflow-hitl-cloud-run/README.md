# Example 06 (Part 3): Resumable 6-Node ADK Workflow with Human-in-the-Loop (HITL) on Cloud Run & `VertexAiSessionService`

When an ADK `Workflow` discovers a stopped (`TERMINATED`) Compute Engine VM that needs live remediation, running inside a stateless **Cloud Run** container introduces two architectural requirements:

1. **Non-Blocking Approval Gate**: HTTP Request 1 (`POST /run`) cannot block waiting minutes or hours for a human operator to approve starting a VM.
2. **Serverless Checkpoint Persistence Across Container Instances**: Between **HTTP Request 1** (the audit that pauses for approval) and **HTTP Request 2** (the operator's approval or rejection), Cloud Run may scale the container to zero or route Request 2 to a different container instance.

This directory implements a **6-Node Resumable ADK `Workflow`** ([`app/agent.py`](app/agent.py)) served by FastAPI ([`server.py`](server.py)) on **Cloud Run** and backed by the **Vertex AI Agent Engine Session Service (`VertexAiSessionService`)**.

---

## Architecture: The 6-Node Resumable HITL Workflow

```text
START (HTTP Request 1: POST /run)
  │
  ▼
[Node 1: init_audit_context] (FunctionNode)
  │
  ▼
[Node 2: vm_discovery_agent] (LlmAgent -> Remote MCP `compute instances list`)
  │
  ▼
[Node 3: evaluate_and_route] (FunctionNode)
  ├── route == "healthy" (0 TERMINATED VMs)
  │     └──► [Node 4A: healthy_compliance_agent] (Single-Request Finish)
  │
  └── route == "needs_remediation" (>= 1 TERMINATED VMs)
        └──► [Node 4B: request_remediation_approval] (@node(rerun_on_resume=True))
               │
               ├── Pass 1 (HTTP Request 1):
               │     yield RequestInput(interrupt_id="approve_vm_remediation")
               │     💾 Checkpointed to VertexAiSessionService (`agentengine://...`)
               │     ⏸️ HTTP Request 1 returns 200 OK (Cloud Run can scale to zero)
               │
               └── Pass 2 (HTTP Request 2: POST /run with functionResponse):
                     ⚡ Fast-forwards past Nodes 1–3 (no duplicate MCP discovery!)
                     ├── route == "approved" ──► [Node 5A: remediation_executor_agent]
                     │                             (Labels VM & Disk + Starts VM via Remote MCP)
                     └── route == "rejected" ──► [Node 5B: remediation_rejected_node]
                                                   (Audit Log: Leaves VM TERMINATED)
```

---

## Step 1: Provision the `VertexAiSessionService` Store

Run [`setup_session_engine.py`](setup_session_engine.py) once to create (or reuse) a lightweight, containerless `ReasoningEngine` resource in Vertex AI that serves as the parent session store (`projects/{project}/locations/{region}/reasoningEngines/{agent_engine_id}/sessions/{session_id}`):

```bash
export GOOGLE_CLOUD_PROJECT="your-project-id"

uv run examples/06-adk-workflow-hitl-cloud-run/setup_session_engine.py \
  --project="${GOOGLE_CLOUD_PROJECT}" \
  --region=us-central1
```

Export the returned `GOOGLE_CLOUD_AGENT_ENGINE_ID`:

```bash
export GOOGLE_CLOUD_AGENT_ENGINE_ID="YOUR_AGENT_ENGINE_ID"
export AGENT_ENGINE_LOCATION="us-central1"
```

---

## Step 2: Run & Test Locally Against Live `VertexAiSessionService`

Start the FastAPI server locally:

```bash
cd examples/06-adk-workflow-hitl-cloud-run
uv run uvicorn server:app --host 127.0.0.1 --port 8080
```

In a second terminal, run [`run_hitl_client.py`](run_hitl_client.py) to test all three branches:

```bash
# 1. Test Pause & Reject (leaves demo-web-server TERMINATED)
uv run examples/06-adk-workflow-hitl-cloud-run/run_hitl_client.py \
  --url=http://127.0.0.1:8080 \
  --decision=reject \
  --reason="Maintenance window in progress; keep VM stopped."

# 2. Test Pause & Approve (labels VM + disk and starts demo-web-server)
uv run examples/06-adk-workflow-hitl-cloud-run/run_hitl_client.py \
  --url=http://127.0.0.1:8080 \
  --decision=approve \
  --reason="Approved to restart demo-web-server and apply compliance labels."

# 3. Test Healthy Single-Request Completion (when demo-web-server is RUNNING)
uv run examples/06-adk-workflow-hitl-cloud-run/run_hitl_client.py \
  --url=http://127.0.0.1:8080
```

---

## Step 3: Deploy to Cloud Run & Test Remotely

Deploy the service to Cloud Run with a least-privilege Service Account (`roles/aiplatform.user`, `roles/mcp.toolUser`, `roles/compute.instanceAdmin.v1`):

```bash
cd examples/06-adk-workflow-hitl-cloud-run
chmod +x deploy.sh
./deploy.sh
```

Then run [`run_hitl_client.py`](run_hitl_client.py) against your deployed Cloud Run URL (it automatically attaches `Authorization: Bearer $(gcloud auth print-identity-token)`):

```bash
uv run examples/06-adk-workflow-hitl-cloud-run/run_hitl_client.py \
  --url="https://gcloud-mcp-hitl-workflow-YOUR-HASH-uc.a.run.app" \
  --decision=approve
```
