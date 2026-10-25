#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "google-auth>=2.35.0",
#     "google-genai>=2.3.0",
#     "pydantic>=2.10.0",
#     "requests>=2.32.0",
# ]
# ///
"""Managed Agents & Interactions API Runner for Long-Running gcloud Commands.

Demonstrates how to execute, poll, and verify long-running `gcloud` commands
(`--async`) on the Gemini Enterprise Agent Platform (`google-genai >= 2.3.0`)
with the Google Cloud CLI Remote MCP Server (`https://cloudcli.googleapis.com/mcp`):

  1. Control Plane (Managed Agents API):
     Registers `https://cloudcli.googleapis.com/mcp` in Google Cloud Agent Registry
     and provisions the `cloud-cli-lro-agent` Managed Agent resource on
     `aiplatform.googleapis.com/v1beta1`, storing the 3-Phase LRO system instruction
     and MCP server binding server-side. Retrieves the provisioned agent via
     `client.agents.get("cloud-cli-lro-agent")`.

  2. Data Plane (Gemini Interactions API — Stateful Multi-Turn & Background Execution):
     Executes the 3-Phase LRO Protocol using `client.interactions.create(...)` with
     server-side state retention (`store=True`, `previous_interaction_id`):
       - Phase 1 (Submit with `--async`): Calls `run_gcloud_command` on
         `https://cloudcli.googleapis.com/mcp` with `--async --format=json --quiet`
         and extracts the `operation-...` identifier immediately.
       - Phase 2 (Poll Operation until `DONE`): Polls
         `gcloud compute operations describe <operation_name> ... --format=json --quiet`
         across stateful interaction turns until `status == "DONE"`.
       - Phase 3 (Verify Resource State): Queries
         `gcloud compute instances describe <vm_name> ...` and verifies that the VM
         reached its expected terminal state (`RUNNING` or `TERMINATED`).

Usage:
  export GOOGLE_CLOUD_PROJECT="your-project-id"
  uv run examples/07-long-running-commands/run_managed_lro.py \
    --project="${GOOGLE_CLOUD_PROJECT}" \
    --action=stop
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

import requests
from pydantic import BaseModel, Field

DEFAULT_PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "YOUR_PROJECT_ID")
DEFAULT_LOCATION = "global"
DEFAULT_ZONE = os.environ.get("GOOGLE_CLOUD_ZONE", "us-central1-a")
DEFAULT_VM_NAME = os.environ.get("TARGET_VM_NAME", "demo-web-server")
DEFAULT_AGENT_ID = "cloud-cli-lro-agent"
DEFAULT_MODEL = "gemini-3-flash-preview"
GCLOUD_MCP_URL = "https://cloudcli.googleapis.com/mcp"


class LROVerificationSummary(BaseModel):
    """Structured output schema for verifying a completed Long-Running Operation."""

    operation_name: str = Field(
        description="Name of the Google Cloud Operation (e.g., operation-...)."
    )
    operation_status: str = Field(
        description="Final status of the operation from `gcloud compute operations describe` (must be DONE)."
    )
    vm_name: str = Field(description="Name of the target Compute Engine VM.")
    actual_vm_status: str = Field(
        description="Actual VM status from `gcloud compute instances describe` (e.g., RUNNING or TERMINATED)."
    )
    expected_vm_status: str = Field(
        description="Expected VM status after the operation completed (RUNNING or TERMINATED)."
    )
    verification_passed: bool = Field(
        description="True if and only if operation_status == 'DONE' and actual_vm_status == expected_vm_status."
    )
    summary: str = Field(
        description="Concise summary of the async submission, polling, and verification steps."
    )


def get_access_token() -> str:
    """Retrieves an OAuth 2.0 access token via ADC, environment, or gcloud."""
    env_token = os.environ.get("GCP_ACCESS_TOKEN")
    if env_token:
        return env_token.strip()

    try:
        import google.auth
        import google.auth.transport.requests

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(google.auth.transport.requests.Request())
        if credentials.token:
            return credentials.token
    except Exception:
        pass

    gcloud_bin = shutil.which("gcloud")
    if not gcloud_bin:
        raise RuntimeError("Could not find 'gcloud' binary on PATH.")

    res = subprocess.run(
        [gcloud_bin, "auth", "print-access-token", "--quiet"],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return res.stdout.strip()


def redact_tokens(obj: Any) -> Any:
    """Redacts Bearer tokens from dicts/lists before printing."""
    cloned = copy.deepcopy(obj)
    if isinstance(cloned, dict):
        for k, v in cloned.items():
            if (
                k.lower() == "authorization"
                and isinstance(v, str)
                and v.startswith("Bearer ")
            ):
                cloned[k] = "Bearer <REDACTED_OAUTH_ACCESS_TOKEN>"
            else:
                cloned[k] = redact_tokens(v)
    elif isinstance(cloned, list):
        return [redact_tokens(item) for item in cloned]
    return cloned


def api_request(
    method: str,
    url: str,
    token: str,
    project_id: str,
    payload: dict[str, Any] | None = None,
    quiet_errors: bool = False,
) -> dict[str, Any]:
    """Executes an authenticated REST request against Google Cloud APIs."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "x-goog-user-project": project_id,
            "Content-Type": "application/json; charset=utf-8",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        if not quiet_errors:
            print(f"HTTP {exc.code} from {url}:\n{err_body}")
        raise


def ensure_agent_registry_mcp_server(
    project_id: str, location: str, service_id: str, token: str
) -> str:
    """Ensures cloudcli.googleapis.com/mcp is registered in Agent Registry."""
    service_url = (
        f"https://agentregistry.googleapis.com/v1alpha/projects/{project_id}"
        f"/locations/{location}/services/{service_id}"
    )
    try:
        existing = api_request(
            "GET", service_url, token, project_id, quiet_errors=True
        )
        if existing.get("registryResource"):
            return existing["registryResource"]
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise

    create_url = (
        f"https://agentregistry.googleapis.com/v1alpha/projects/{project_id}"
        f"/locations/{location}/services?serviceId={service_id}"
    )
    service_payload = {
        "displayName": "cloudcli.googleapis.com",
        "description": "Google Cloud CLI Remote MCP Server (run_gcloud_command, run_bq_command)",
        "interfaces": [
            {
                "url": GCLOUD_MCP_URL,
                "protocolBinding": "JSONRPC",
            }
        ],
        "mcpServerSpec": {
            "type": "NO_SPEC",
        },
    }
    op = api_request("POST", create_url, token, project_id, service_payload)
    if op.get("done") and op.get("response", {}).get("registryResource"):
        return op["response"]["registryResource"]

    op_name = op.get("name")
    if op_name:
        poll_url = f"https://agentregistry.googleapis.com/v1alpha/{op_name}"
        for _ in range(12):
            status = api_request("GET", poll_url, token, project_id)
            if status.get("done"):
                reg_res = status.get("response", {}).get("registryResource")
                if reg_res:
                    return reg_res
                break
            time.sleep(2)

    svc = api_request("GET", service_url, token, project_id)
    return svc["registryResource"]


def provision_lro_managed_agent(
    project_id: str, location: str, agent_id: str
) -> dict[str, Any]:
    """Provisions or updates the LRO-aware Managed Agent on the Control Plane."""
    token = get_access_token()
    mcp_registry_name = ensure_agent_registry_mcp_server(
        project_id, location, "gcloud-remote-mcp", token
    )
    endpoint = (
        f"https://aiplatform.googleapis.com/v1beta1/projects/{project_id}"
        f"/locations/{location}/agents"
    )

    system_instruction = (
        f"You are a Google Cloud Operations LRO specialist for project '{project_id}'. "
        f"Whenever you execute a mutating or long-running gcloud command via `run_gcloud_command`, "
        f"you MUST follow this strict 3-Phase Long-Running Operation (LRO) Protocol:\n"
        f"1. PHASE 1 (SUBMIT WITH --async): Run the command with `--async --format=json --quiet` "
        f"(and `project='projects/{project_id}'`, `--project={project_id}`, using '=' for all flags) "
        f"and extract the returned `operation-...` name.\n"
        f"2. PHASE 2 (POLL OPERATION UNTIL DONE): Call `run_gcloud_command` with "
        f"`gcloud compute operations describe <OPERATION_NAME> --zone=<ZONE> --project={project_id} --format=json --quiet` "
        f"until the operation's `status` is `DONE` and confirm no `error` field is present.\n"
        f"3. PHASE 3 (VERIFY TARGET RESOURCE): Call `run_gcloud_command` with "
        f"`gcloud compute instances describe <VM_NAME> --zone=<ZONE> --project={project_id} --format=\"json(name,status,lastStartTimestamp,lastStopTimestamp)\" --quiet` "
        f"to confirm the resource reached the expected state.\n"
        f"Finally, end your response with a JSON code block containing keys: "
        f"`operation_name`, `operation_status`, `vm_name`, `actual_vm_status`, `expected_vm_status`, `verification_passed` (boolean), and `summary`."
    )

    payload = {
        "id": agent_id,
        "base_agent": "antigravity-preview-05-2026",
        "description": (
            "Managed Cloud Operations Agent that executes long-running gcloud commands "
            "with --async, polls the operation until DONE, and verifies final resource state."
        ),
        "system_instruction": system_instruction,
        "tools": [
            {
                "type": "mcp_server",
                "name": mcp_registry_name,
                "url": GCLOUD_MCP_URL,
                "headers": {
                    "Authorization": f"Bearer {token}",
                },
            }
        ],
        "base_environment": {
            "type": "remote",
            "network": {"allowlist": [{"domain": "*"}]},
        },
    }

    try:
        existing = api_request(
            "GET", f"{endpoint}/{agent_id}", token, project_id, quiet_errors=True
        )
        if existing.get("name"):
            print(
                f"Updating existing Managed Agent '{agent_id}' on Control Plane (?update_mask=system_instruction)..."
            )
            updated = api_request(
                "PATCH",
                f"{endpoint}/{agent_id}?update_mask=system_instruction",
                token,
                project_id,
                {
                    "name": agent_id,
                    "system_instruction": system_instruction,
                },
            )
            return updated
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise

    print(f"Provisioning LRO Managed Agent '{agent_id}' on Control Plane...")
    op = api_request("POST", endpoint, token, project_id, payload)
    op_name = op.get("name")
    if op_name:
        poll_url = f"https://aiplatform.googleapis.com/v1beta1/{op_name}"
        for _ in range(15):
            time.sleep(2)
            status = api_request("GET", poll_url, token, project_id)
            if status.get("done"):
                return status.get("response", {})
    return api_request("GET", f"{endpoint}/{agent_id}", token, project_id)


def invoke_remote_mcp_gcloud(project_id: str, command: str) -> str:
    """Calls `run_gcloud_command` on `https://cloudcli.googleapis.com/mcp` over JSON-RPC 2.0."""
    token = get_access_token()
    resp = requests.post(
        GCLOUD_MCP_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "x-goog-user-project": project_id,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "run_gcloud_command",
                "arguments": {
                    "command": command,
                    "project": f"projects/{project_id}",
                },
            },
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    content_blocks = data.get("result", {}).get("content", [])
    return "\n".join(b.get("text", "") for b in content_blocks if "text" in b)


def parse_gcloud_mcp_json(mcp_output: str) -> dict[str, Any]:
    """Extracts the parsed gcloud JSON object from a Remote MCP tool response."""
    raw = str(mcp_output or "").strip()
    if not raw:
        return {}
    try:
        outer = json.loads(raw)
        if isinstance(outer, dict) and isinstance(outer.get("stdout"), str):
            raw = outer["stdout"].strip()
        elif isinstance(outer, dict):
            return outer
        elif isinstance(outer, list) and outer and isinstance(outer[0], dict):
            return outer[0]
    except json.JSONDecodeError:
        pass

    for start_char, end_char in (("[", "]"), ("{", "}")):
        start_idx = raw.find(start_char)
        end_idx = raw.rfind(end_char)
        if start_idx != -1 and end_idx > start_idx:
            try:
                parsed = json.loads(raw[start_idx : end_idx + 1])
                if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                    return parsed[0]
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
    return {}


def extract_json_verification(text: str) -> dict[str, Any]:
    """Extracts the structured JSON verification block from the agent's output."""
    cleaned = str(text or "").strip()
    try:
        direct = json.loads(cleaned)
        if isinstance(direct, dict) and "verification_passed" in direct:
            return direct
    except json.JSONDecodeError:
        pass

    fences = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    for block in reversed(fences):
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict) and "verification_passed" in parsed:
                return parsed
        except json.JSONDecodeError:
            continue

    matches = re.findall(
        r"\{[^{}]*\"verification_passed\"[^{}]*\}", cleaned, re.DOTALL
    )
    for block in reversed(matches):
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return {}


def run_stateful_interactions_lro(
    client: Any,
    project_id: str,
    system_instruction: str,
    prompt: str,
    poll_interval: int,
    max_turns: int = 15,
) -> tuple[str, str]:
    """Executes the 3-Phase LRO workflow over the Stateful Gemini Interactions API.

    Uses `store=True` and `previous_interaction_id` to maintain conversation state
    in the cloud while dispatching `run_gcloud_command` tool calls to
    `https://cloudcli.googleapis.com/mcp`.
    """

    run_gcloud_tool = {
        "type": "function",
        "name": "run_gcloud_command",
        "description": (
            "Executes a single gcloud CLI command on the Google Cloud CLI Remote MCP Server "
            "(https://cloudcli.googleapis.com/mcp)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "Full gcloud command string (e.g., 'gcloud compute instances stop "
                        "demo-web-server --zone=us-central1-a --project=my-project --async --format=json --quiet')."
                    ),
                },
                "project": {
                    "type": "string",
                    "description": "Project resource path in the format 'projects/PROJECT_ID'.",
                },
            },
            "required": ["command", "project"],
        },
    }

    print("=== Data Plane: Starting Stateful Interaction (store=True) ===")
    interaction = client.interactions.create(
        model=DEFAULT_MODEL,
        system_instruction=system_instruction,
        tools=[run_gcloud_tool],
        input=prompt,
        store=True,
    )
    print(
        f"  [Turn 1] interaction.id={interaction.id} | status={interaction.status}"
    )

    poll_attempt = 0
    for turn_idx in range(2, max_turns + 1):
        function_calls = [
            s
            for s in (interaction.steps or [])
            if getattr(s, "type", None) == "function_call"
        ]
        if not function_calls:
            break

        tool_results: list[dict[str, Any]] = []
        for fc in function_calls:
            cmd = (fc.arguments or {}).get("command", "")
            is_op_poll = "operations describe" in cmd
            if is_op_poll:
                poll_attempt += 1
                print(
                    f"  [Turn {turn_idx - 1} -> Phase 2 Poll #{poll_attempt}] Remote MCP: `{cmd}`"
                )
            elif "--async" in cmd:
                print(f"  [Turn {turn_idx - 1} -> Phase 1 Async Submit] Remote MCP: `{cmd}`")
            else:
                print(f"  [Turn {turn_idx - 1} -> Phase 3 Verify Resource] Remote MCP: `{cmd}`")

            mcp_output = invoke_remote_mcp_gcloud(project_id, cmd)

            # Parse operation status for clean logging and controlled backoff if still RUNNING
            parsed_mcp = parse_gcloud_mcp_json(mcp_output)
            if is_op_poll:
                op_status = parsed_mcp.get("status", "DONE")
                op_progress = parsed_mcp.get("progress", 100)
                print(
                    f"    -> Operation Status: {op_status} (progress={op_progress}%)"
                )
                if op_status in ("PENDING", "RUNNING"):
                    print(
                        f"    -> Operation still {op_status}; sleeping {poll_interval}s before next poll turn..."
                    )
                    time.sleep(poll_interval)
            elif "--async" in cmd:
                op_name = parsed_mcp.get("name") or re.search(
                    r"(operation-[a-zA-Z0-9\-]+)", mcp_output
                )
                if hasattr(op_name, "group"):
                    op_name = op_name.group(1)
                print(
                    f"    -> Captured Operation: {op_name} "
                    f"(operationType={parsed_mcp.get('operationType', 'compute')}, "
                    f"status={parsed_mcp.get('status', 'RUNNING')})"
                )
            else:
                print(
                    f"    -> Target VM '{parsed_mcp.get('name', 'demo-web-server')}' "
                    f"actual status: {parsed_mcp.get('status', 'VERIFIED')}"
                )

            tool_results.append(
                {
                    "type": "function_result",
                    "name": fc.name,
                    "call_id": fc.id,
                    "result": [{"type": "text", "text": mcp_output}],
                }
            )

        interaction = client.interactions.create(
            model=DEFAULT_MODEL,
            system_instruction=system_instruction,
            tools=[run_gcloud_tool],
            input=tool_results,
            previous_interaction_id=interaction.id,
            store=True,
        )
        print(
            f"  [Turn {turn_idx}] interaction.id={interaction.id} | status={interaction.status}"
        )

    return interaction.id, (interaction.output_text or "")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Execute and verify a long-running gcloud command (--async) using Managed Agents & Interactions API."
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--zone", default=DEFAULT_ZONE)
    parser.add_argument("--instance", "--vm-name", dest="vm_name", default=DEFAULT_VM_NAME)
    parser.add_argument("--agent-id", default=DEFAULT_AGENT_ID)
    parser.add_argument(
        "--action",
        choices=["start", "stop"],
        default="stop",
        help="Long-running VM operation to perform with --async: 'start' or 'stop' (default: stop).",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=3,
        help="Seconds to wait between operation status polls when status is RUNNING (default: 3).",
    )
    parser.add_argument(
        "--mode",
        choices=["interactions", "remote-sandbox"],
        default="interactions",
        help=(
            "Execution mode on the Data Plane: 'interactions' (stateful multi-turn "
            "Interactions API loop using the Managed Agent's configuration) or "
            "'remote-sandbox' (background=True remote sandbox execution)."
        ),
    )
    args = parser.parse_args()

    if args.project in ("YOUR_PROJECT_ID", "your-project-id", ""):
        raise SystemExit(
            "Error: Please set GOOGLE_CLOUD_PROJECT or pass --project=your-project-id"
        )

    expected_status = "RUNNING" if args.action == "start" else "TERMINATED"

    print("=" * 78)
    print("Managed Agent & Interactions API : LRO Execution & Verification")
    print(f"Target Project                   : {args.project}")
    print(f"Target Zone                      : {args.zone}")
    print(f"Target VM                        : {args.vm_name}")
    print(
        f"Requested LRO Action             : {args.action} (--async) -> Expect '{expected_status}'"
    )
    print(f"Data Plane Mode                  : {args.mode}")
    print("=" * 78)

    # 1. Control Plane: Provision / Update the Managed Agent & Retrieve via SDK
    provision_lro_managed_agent(args.project, args.location, args.agent_id)

    from google import genai

    client = genai.Client(
        enterprise=True, project=args.project, location=args.location
    )
    managed_agent = client.agents.get(args.agent_id)
    print(
        f"Control Plane Managed Agent Ready: {managed_agent.name} (base_agent={managed_agent.base_agent})\n"
    )

    prompt = (
        f"Execute the 3-Phase LRO Protocol on VM '{args.vm_name}' in zone '{args.zone}' "
        f"(project '{args.project}'):\n"
        f"1. Run `gcloud compute instances {args.action} {args.vm_name} --zone={args.zone} "
        f"--project={args.project} --async --format=json --quiet` via `run_gcloud_command` "
        f"(with `project='projects/{args.project}'`) and extract the `operation-...` name.\n"
        f"2. Poll `gcloud compute operations describe <OPERATION_NAME> --zone={args.zone} "
        f"--project={args.project} --format=json --quiet` until its `status` is `DONE`.\n"
        f"3. Verify the VM state using `gcloud compute instances describe {args.vm_name} "
        f"--zone={args.zone} --project={args.project} "
        f"--format=\"json(name,status,lastStartTimestamp,lastStopTimestamp)\" --quiet` "
        f"to confirm `status` is `{expected_status}`.\n"
        f"End with a JSON code block containing `operation_name`, `operation_status`, "
        f"`vm_name`, `actual_vm_status`, `expected_vm_status` ('{expected_status}'), "
        f"`verification_passed` (boolean), and `summary`."
    )

    interaction_id = ""
    output_text = ""

    if args.mode == "remote-sandbox":
        print(
            "=== Data Plane: Starting Background Interaction (environment=remote, background=True) ==="
        )
        interaction = client.interactions.create(
            agent=managed_agent.name,
            environment={"type": "remote"},
            background=True,
            input=prompt,
        )
        interaction_id = interaction.id
        print(
            f"Interaction started: id={interaction.id} | status={interaction.status}"
        )
        for poll_idx in range(1, 36):
            time.sleep(args.poll_interval)
            current = client.interactions.get(interaction.id)
            print(f"  [Interaction Poll {poll_idx}] status={current.status}")
            if current.status == "completed":
                output_text = current.output_text or ""
                break
            if current.status in ("failed", "cancelled"):
                print(
                    f"\nRemote sandbox returned status='{current.status}'; "
                    f"falling back to Stateful Interactions API mode..."
                )
                break

    if not output_text:
        interaction_id, output_text = run_stateful_interactions_lro(
            client=client,
            project_id=args.project,
            system_instruction=managed_agent.system_instruction or "",
            prompt=prompt,
            poll_interval=args.poll_interval,
        )

    print(f"\n=== Final Agent Response ===\n{output_text}")

    verification_obj = extract_json_verification(output_text)
    actual_vm_status = str(verification_obj.get("actual_vm_status", "")).upper()
    op_status = str(verification_obj.get("operation_status", "")).upper()
    verification_passed = (
        bool(verification_obj.get("verification_passed"))
        and op_status == "DONE"
        and actual_vm_status == expected_status
    )

    print("\n" + "=" * 78)
    print("Managed Agent & Interactions API LRO Verification Summary:")
    print("=" * 78)
    print(f"  Managed Agent Resource: {managed_agent.name}")
    print(f"  Final Interaction ID  : {interaction_id}")
    print(f"  Operation Name        : {verification_obj.get('operation_name')}")
    print(f"  Operation Final State : {op_status}")
    print(f"  Expected VM Status    : {expected_status}")
    print(f"  Verified VM Status    : {actual_vm_status}")
    print(f"  Verification Passed   : {verification_passed}")
    print("=" * 78)

    if not verification_passed:
        raise SystemExit(
            f"ERROR: Managed Agent LRO verification failed! Parsed block: {verification_obj}"
        )


if __name__ == "__main__":
    main()
