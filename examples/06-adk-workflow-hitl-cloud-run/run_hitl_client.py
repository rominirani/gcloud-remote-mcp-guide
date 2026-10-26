#!/usr/bin/env python3
"""2-Request Human-in-the-Loop (HITL) Client for the Part 3 ADK Workflow.

Works identically against:
  - A local FastAPI server (`http://127.0.0.1:8080`)
  - A deployed Cloud Run service (`https://gcloud-mcp-hitl-workflow-...a.run.app`)

Demonstrates all three execution flows backed by `VertexAiSessionService`:
  1. HTTP Request 1 (`POST /run`):
     Starts the audit workflow. If a `TERMINATED` VM is discovered, Node 4B
     (`request_remediation_approval`) emits an `adk_request_input` function call
     with `id="approve_vm_remediation"`, checkpoints session state & events to
     `VertexAiSessionService`, and finishes HTTP Request 1 (`200 OK`).
  2. Human Operator Decision (`approve` or `reject`, interactive or CLI flag):
     Displays the paused interrupt payload and captures the operator's decision.
  3. HTTP Request 2 (`POST /run` with `functionResponse` on the same `sessionId`):
     Resumes the workflow from `VertexAiSessionService` without re-running
     Nodes 1–3, routing to either `remediation_executor_agent` (`approved`) or
     `remediation_rejected_node` (`rejected`).

Usage:
  # Reject remediation
  uv run examples/06-adk-workflow-hitl-cloud-run/run_hitl_client.py \
    --url=http://127.0.0.1:8080 \
    --decision=reject \
    --reason="Maintenance window in progress; keep VM stopped."

  # Approve remediation
  uv run examples/06-adk-workflow-hitl-cloud-run/run_hitl_client.py \
    --url=http://127.0.0.1:8080 \
    --decision=approve \
    --reason="Approved to restart demo-web-server and apply compliance labels."
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx


def get_cloud_run_id_token() -> str | None:
    """Fetches a Google-signed Identity Token for authenticated Cloud Run URLs."""
    gcloud_bin = shutil.which("gcloud")
    if not gcloud_bin:
        return None
    res = subprocess.run(
        [gcloud_bin, "auth", "print-identity-token", "--quiet"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip()
    return None


def print_events_and_find_interrupt(
    events: list[dict[str, Any]], request_label: str
) -> dict[str, Any] | None:
    """Pretty-prints ADK workflow events and returns any `adk_request_input` functionCall."""
    print(f"\n--- {request_label}: Received {len(events)} Event(s) ---")
    pending_interrupt: dict[str, Any] | None = None

    for idx, ev in enumerate(events, start=1):
        author = ev.get("author", "unknown")
        actions = ev.get("actions") or {}
        route = actions.get("route")
        state_delta = actions.get("stateDelta") or actions.get("state_delta") or {}
        output = ev.get("output")

        print(f"\n  [Event {idx}] author='{author}'")
        if route:
            print(f"    route       : {route!r}")
        if state_delta:
            keys = list(state_delta.keys())
            print(f"    state_delta : keys={keys}")
        if output is not None:
            out_str = (
                json.dumps(output)
                if isinstance(output, (dict, list))
                else str(output)
            )
            if len(out_str) > 220:
                out_str = out_str[:220] + "..."
            print(f"    output      : {out_str}")

        content = ev.get("content") or {}
        for part in content.get("parts") or []:
            fc = part.get("functionCall") or part.get("function_call")
            if fc:
                fc_name = fc.get("name", "")
                fc_id = fc.get("id", "")
                fc_args = fc.get("args", {})
                print(
                    f"    functionCall: {fc_name}(id={fc_id!r}, args={json.dumps(fc_args)[:200]})"
                )
                if fc_name == "adk_request_input":
                    pending_interrupt = {
                        "id": fc_id,
                        "name": fc_name,
                        "args": fc_args,
                    }
            fr = part.get("functionResponse") or part.get("function_response")
            if fr:
                print(
                    f"    funcResponse: {fr.get('name')}(id={fr.get('id')!r})"
                )
            text = part.get("text")
            if text:
                print(f"    text        : {text.strip()}")

    return pending_interrupt


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the 2-Request HITL Cloud Audit & Remediation Client."
    )
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8080",
        help="Base URL of the local FastAPI server or deployed Cloud Run service.",
    )
    parser.add_argument("--app-name", default="app")
    parser.add_argument("--user-id", default="sre-operator")
    parser.add_argument(
        "--decision",
        choices=["approve", "reject", "timeout", "interactive"],
        default="interactive",
        help="Decision when HITL pause is triggered: 'approve', 'reject', 'timeout' (nobody answers), or 'interactive'.",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=int(os.environ.get("HITL_APPROVAL_TTL_SECONDS", "900")),
        help="Approval TTL in seconds before a request expires (default: 900s).",
    )
    parser.add_argument(
        "--default-timeout-action",
        choices=["reject", "approve"],
        default=os.environ.get("HITL_DEFAULT_TIMEOUT_ACTION", "reject"),
        help="Default action executed when nobody answers within --ttl-seconds (default: 'reject').",
    )
    parser.add_argument(
        "--simulate-delay-seconds",
        type=float,
        default=0.0,
        help="Seconds to sleep before sending HTTP Request 2 (use to test stale approval expiry).",
    )
    parser.add_argument(
        "--approver",
        default="romin-sre-lead",
        help="Operator identifier recorded in the audit log.",
    )
    parser.add_argument(
        "--reason",
        default="",
        help="Approval or rejection comment recorded in session state.",
    )
    parser.add_argument(
        "--prompt",
        default="Audit Compute Engine VMs in us-central1-a and remediate any stopped VMs.",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
        help="Google Cloud Project ID (used when --spawn-local-server is set).",
    )
    parser.add_argument(
        "--agent-engine-id",
        default=os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID", ""),
        help="Vertex AI ReasoningEngine ID for VertexAiSessionService (used when --spawn-local-server is set).",
    )
    parser.add_argument(
        "--spawn-local-server",
        action="store_true",
        help="Automatically start a local uvicorn server:app instance for the test run.",
    )
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    server_proc: subprocess.Popen[str] | None = None

    if args.spawn_local_server:
        env = os.environ.copy()
        if args.project:
            env["GOOGLE_CLOUD_PROJECT"] = args.project
        if args.agent_engine_id:
            env["GOOGLE_CLOUD_AGENT_ENGINE_ID"] = args.agent_engine_id
        env.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")
        env.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
        env["HITL_APPROVAL_TTL_SECONDS"] = str(args.ttl_seconds)
        env["HITL_DEFAULT_TIMEOUT_ACTION"] = str(args.default_timeout_action)

        server_dir = Path(__file__).resolve().parent
        port = base_url.rsplit(":", 1)[-1] if ":" in base_url.split("//")[-1] else "8080"
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "server:app",
                "--host",
                "127.0.0.1",
                "--port",
                port,
            ],
            cwd=str(server_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # Wait for /healthz
        for _ in range(30):
            try:
                r = httpx.get(f"{base_url}/healthz", timeout=2.0)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            out = server_proc.stdout.read() if server_proc.stdout else ""
            server_proc.terminate()
            raise RuntimeError(f"Local uvicorn server failed to start:\n{out}")

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if base_url.startswith("https://"):
        id_token = get_cloud_run_id_token()
        if id_token:
            headers["Authorization"] = f"Bearer {id_token}"

    try:
        with httpx.Client(base_url=base_url, headers=headers, timeout=180.0) as client:
            # 0. Check server health & session backend
            health_resp = client.get("/healthz")
            health_resp.raise_for_status()
            health_info = health_resp.json()

            print("=" * 78)
            print("Part 3 HITL Client : Cloud Run + VertexAiSessionService + Remote MCP")
            print(f"Target Endpoint    : {base_url}")
            print(f"Session Backend    : {health_info.get('session_backend')}")
            print(f"Session URI        : {health_info.get('session_service_uri')}")
            print("=" * 78)

            # 1. Create a new session in VertexAiSessionService
            session_resp = client.post(
                f"/apps/{args.app_name}/users/{args.user_id}/sessions",
                json={},
            )
            session_resp.raise_for_status()
            session_data = session_resp.json()
            session_id = session_data["id"]
            print(f"\n[Session Created in VertexAiSessionService] sessionId={session_id}")

            # 2. HTTP Request 1: Start the Workflow Audit
            print("\n=== HTTP Request 1: POST /run (Initiate VM Discovery & Evaluation) ===")
            req1_payload = {
                "appName": args.app_name,
                "userId": args.user_id,
                "sessionId": session_id,
                "newMessage": {
                    "role": "user",
                    "parts": [{"text": args.prompt}],
                },
            }
            run1_resp = client.post("/run", json=req1_payload)
            run1_resp.raise_for_status()
            events1 = run1_resp.json()

            interrupt = print_events_and_find_interrupt(events1, "HTTP Request 1")

            if interrupt is None:
                print(
                    "\n[Single-Request Completion] All VMs are RUNNING (healthy branch). "
                    "No HITL interrupt was emitted!"
                )
            else:
                # 3. Workflow Paused at Node 4B (`request_remediation_approval`)
                int_id = interrupt["id"]
                int_args = interrupt["args"]
                print("\n" + "!" * 78)
                print("⏸️  WORKFLOW PAUSED FOR HUMAN-IN-THE-LOOP (HITL) APPROVAL")
                print("!" * 78)
                print(f"  Interrupt ID : {int_id}")
                print(f"  Message      : {int_args.get('message')}")
                print(
                    f"  Payload      : {json.dumps(int_args.get('payload'), indent=2)}"
                )
                print("!" * 78)

                if args.simulate_delay_seconds > 0:
                    print(
                        f"\n  [Simulating Delay] Waiting {args.simulate_delay_seconds}s before sending HTTP Request 2..."
                    )
                    time.sleep(args.simulate_delay_seconds)

                if args.decision == "interactive":
                    ans = input("\nApprove VM remediation? [y/N]: ").strip().lower()
                    approved = ans in ("y", "yes", "approve", "approved")
                    reason = args.reason or input("Optional comment: ").strip()
                    decision_payload: dict[str, Any] = {
                        "approved": approved,
                        "approver": args.approver,
                        "reason": reason,
                    }
                elif args.decision == "timeout":
                    payload_ttl = int(
                        (int_args.get("payload") or {}).get(
                            "ttl_seconds", args.ttl_seconds
                        )
                    )
                    wait_secs = max(0.0, float(payload_ttl))
                    if wait_secs > 0 and args.simulate_delay_seconds == 0:
                        print(
                            f"\n  [Watchdog Timer] Waiting {wait_secs}s for human response before triggering default timeout action..."
                        )
                        time.sleep(wait_secs + 0.2)
                    decision_payload = {
                        "timed_out": True,
                        "approver": "system-ttl-watchdog",
                        "reason": args.reason
                        or (
                            f"No operator response within {payload_ttl}s TTL; "
                            f"executing default_timeout_action='{args.default_timeout_action}'."
                        ),
                    }
                else:
                    approved = args.decision == "approve"
                    reason = args.reason or (
                        "Approved by SRE on-call to restart VM and apply compliance labels."
                        if approved
                        else "Rejected by SRE on-call; VM intentionally stopped for maintenance."
                    )
                    action_digest = str(
                        (int_args.get("payload") or {}).get("action_digest", "")
                    )
                    decision_payload = {
                        "approved": approved,
                        "approver": args.approver,
                        "reason": reason,
                        "action_digest": action_digest,
                    }
                print(
                    f"\n=== HTTP Request 2: POST /run (Resume Session {session_id} with Decision) ==="
                )
                print(f"  Submitting functionResponse: {json.dumps(decision_payload)}")

                req2_payload = {
                    "appName": args.app_name,
                    "userId": args.user_id,
                    "sessionId": session_id,
                    "newMessage": {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "id": int_id,
                                    "name": "adk_request_input",
                                    "response": {
                                        "result": json.dumps(decision_payload)
                                    },
                                }
                            }
                        ],
                    },
                }
                run2_resp = client.post("/run", json=req2_payload)
                run2_resp.raise_for_status()
                events2 = run2_resp.json()

                print_events_and_find_interrupt(events2, "HTTP Request 2 (Resumed)")

            # 4. Fetch Final Persisted Session State from VertexAiSessionService
            final_sess_resp = client.get(
                f"/apps/{args.app_name}/users/{args.user_id}/sessions/{session_id}"
            )
            final_sess_resp.raise_for_status()
            final_state = final_sess_resp.json().get("state") or {}

            print("\n" + "=" * 78)
            print(f"Final Persisted State in VertexAiSessionService (sessionId={session_id}):")
            print("=" * 78)
            for key in (
                "target_project",
                "default_zone",
                "target_vm_name",
                "target_vm_status",
                "approval_ttl_seconds",
                "approval_expires_at_iso",
                "default_timeout_action",
                "approval_status",
                "approved_by",
                "approval_comment",
                "audit_status",
                "risk_level",
                "workflow_execution_result",
            ):
                if key in final_state:
                    print(f"  {key:26s}: {final_state[key]}")
            print("=" * 78)
    finally:
        if server_proc is not None:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_proc.kill()


if __name__ == "__main__":
    main()
