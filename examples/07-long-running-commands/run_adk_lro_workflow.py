#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "google-adk[mcp,gcp]>=2.0.0",
#     "google-auth>=2.35.0",
#     "pydantic>=2.9.0",
# ]
# ///
"""CLI Runner & Verifier for the ADK Long-Running gcloud Command (`--async`) Workflow.

Executes the 7-node ADK `Workflow` in `app/agent.py` against
`https://cloudcli.googleapis.com/mcp`, traces the conditional LRO polling loop,
and deterministically asserts that both the Google Cloud Operation (`DONE`) and
the final Compute Engine VM state (`RUNNING` or `TERMINATED`) succeeded.

Usage:
  export GOOGLE_CLOUD_PROJECT="your-project-id"
  uv run examples/07-long-running-commands/run_adk_lro_workflow.py \
    --project="${GOOGLE_CLOUD_PROJECT}" \
    --action=start
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import warnings

warnings.filterwarnings(
    "ignore", message=r".*\[EXPERIMENTAL\].*", category=UserWarning
)

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


async def async_main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the ADK Long-Running gcloud Command (--async) + LRO Verification Workflow."
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("GOOGLE_CLOUD_PROJECT", "YOUR_PROJECT_ID"),
        help="Target Google Cloud project ID (defaults to GOOGLE_CLOUD_PROJECT).",
    )
    parser.add_argument(
        "--zone",
        default=os.environ.get("GOOGLE_CLOUD_ZONE", "us-central1-a"),
        help="Target Compute Engine zone (default: us-central1-a).",
    )
    parser.add_argument(
        "--vm-name",
        default=os.environ.get("TARGET_VM_NAME", "demo-web-server"),
        help="Target Compute Engine VM instance name (default: demo-web-server).",
    )
    parser.add_argument(
        "--action",
        choices=["start", "stop"],
        default=os.environ.get("LRO_ACTION", "start"),
        help="Long-running VM action to execute with --async: 'start' or 'stop' (default: start).",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=int(os.environ.get("LRO_POLL_INTERVAL_SECONDS", "5")),
        help="Seconds between LRO poll checks (default: 5).",
    )
    parser.add_argument(
        "--max-polls",
        type=int,
        default=int(os.environ.get("LRO_MAX_POLL_ATTEMPTS", "10")),
        help="Maximum LRO poll iterations before timing out (default: 10).",
    )
    args = parser.parse_args()

    if args.project in ("YOUR_PROJECT_ID", "your-project-id", ""):
        raise SystemExit(
            "Error: Please set GOOGLE_CLOUD_PROJECT or pass --project=your-project-id"
        )

    os.environ["GOOGLE_CLOUD_PROJECT"] = args.project
    os.environ["GOOGLE_CLOUD_ZONE"] = args.zone
    os.environ["TARGET_VM_NAME"] = args.vm_name
    os.environ["LRO_ACTION"] = args.action
    os.environ["LRO_POLL_INTERVAL_SECONDS"] = str(args.poll_interval)
    os.environ["LRO_MAX_POLL_ATTEMPTS"] = str(args.max_polls)
    os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
    os.environ.setdefault("ADK_MODEL", "gemini-3-flash-preview")

    from google.adk.apps import App
    from google.adk.runners import InMemoryRunner
    from google.genai import types
    from app.agent import _parse_json_dict, gcloud_mcp_tools, root_agent

    app = App(name="app", root_agent=root_agent)
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name="app", user_id="sre_operator"
    )

    expected_status = "RUNNING" if args.action == "start" else "TERMINATED"
    trigger_prompt = (
        f"Execute 'gcloud compute instances {args.action} {args.vm_name}' "
        f"with --async in zone {args.zone}, poll the operation until DONE, "
        f"and verify the VM reaches {expected_status}."
    )

    print("=" * 78)
    print("Starting ADK Workflow : gcloud_async_lro_verification_workflow")
    print(f"Target Project        : {args.project}")
    print(f"Target Zone           : {args.zone}")
    print(f"Target VM             : {args.vm_name}")
    print(f"Requested LRO Action  : {args.action} (--async) -> Expect '{expected_status}'")
    print(f"Poll Configuration    : interval={args.poll_interval}s, max_polls={args.max_polls}")
    print("=" * 78)

    try:
        async for event in runner.run_async(
            user_id="sre_operator",
            session_id=session.id,
            new_message=types.Content(
                role="user", parts=[types.Part.from_text(text=trigger_prompt)]
            ),
        ):
            author = getattr(event, "author", None) or "workflow"

            # 1. Print MCP tool calls made by LlmAgent nodes
            for fn_call in event.get_function_calls() or []:
                print(
                    f"\n[{author}] -> Tool Call: {fn_call.name}({json.dumps(fn_call.args)})"
                )

            # 2. Print FunctionNode Event.output / route / state_delta
            state_delta = (
                dict(event.actions.state_delta)
                if getattr(event, "actions", None) and event.actions.state_delta
                else {}
            )
            route = (
                getattr(event.actions, "route", None)
                if getattr(event, "actions", None)
                else None
            )
            node_output = getattr(event, "output", None)

            if node_output is not None or route is not None or state_delta:
                print(f"\n[{author}] Event Emitted:")
                if route is not None:
                    print(f"  Route Selected : {route}")
                if node_output is not None:
                    formatted_out = (
                        json.dumps(node_output, indent=2)
                        if isinstance(node_output, (dict, list))
                        else str(node_output)
                    )
                    print(f"  Node Output    : {formatted_out}")
                if state_delta:
                    print(
                        f"  State Updates  : {json.dumps(state_delta, indent=2)}"
                    )

            # 3. Print final text response from LlmAgent nodes
            if event.is_final_response() and event.content and event.content.parts:
                texts = [
                    p.text
                    for p in event.content.parts
                    if getattr(p, "text", None)
                ]
                if texts:
                    print(f"\n[{author}] Final Response:\n" + "\n".join(texts))

        final_session = await runner.session_service.get_session(
            app_name="app", user_id="sre_operator", session_id=session.id
        )
        final_state = dict(final_session.state) if final_session else {}

        print("\n" + "=" * 78)
        print("Final Workflow Session State (`session.state`):")
        print("=" * 78)
        print(json.dumps(final_state, indent=2))

        # Deterministic End-to-End Verification Check
        lro_completed = bool(final_state.get("lro_completed"))
        verification_raw = final_state.get("resource_verification_result", "{}")
        verification_obj = _parse_json_dict(str(verification_raw))
        actual_vm_status = str(verification_obj.get("actual_vm_status", "")).upper()
        verification_passed = (
            lro_completed
            and bool(verification_obj.get("verification_passed"))
            and actual_vm_status == expected_status
        )

        print("\n" + "=" * 78)
        print("Deterministic LRO & Resource Verification Summary:")
        print("=" * 78)
        print(f"  Operation Name        : {final_state.get('operation_name')}")
        print(f"  Operation Final State : {final_state.get('operation_final_status')}")
        print(f"  Poll Attempts Taken   : {final_state.get('poll_attempt')}")
        print(f"  Expected VM Status    : {expected_status}")
        print(f"  Verified VM Status    : {actual_vm_status}")
        print(f"  Verification Passed   : {verification_passed}")
        print("=" * 78)

        if not verification_passed:
            raise SystemExit(
                f"ERROR: Long-running operation verification failed! "
                f"Expected VM status '{expected_status}', got '{actual_vm_status}'."
            )
    finally:
        await gcloud_mcp_tools.close()


if __name__ == "__main__":
    asyncio.run(async_main())
