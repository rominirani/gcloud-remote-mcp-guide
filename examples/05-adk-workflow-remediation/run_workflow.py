#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "google-adk[mcp,gcp]>=2.0.0",
#     "google-auth>=2.35.0",
#     "pydantic>=2.9.0",
# ]
# ///
"""CLI Runner for the ADK Cloud Audit & Live Remediation Workflow.

Executes the 4-node ADK `Workflow` defined in `app/agent.py` against the
Google Cloud CLI Remote MCP Server (`https://cloudcli.googleapis.com/mcp`)
and prints each node's Event output, route, tool calls, and state updates.

Usage:
  export GOOGLE_CLOUD_PROJECT="your-project-id"
  uv run examples/05-adk-workflow-remediation/run_workflow.py --project="${GOOGLE_CLOUD_PROJECT}"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Any
import warnings

warnings.filterwarnings(
    "ignore", message=r".*\[EXPERIMENTAL\].*", category=UserWarning
)

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


async def async_main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the 4-node ADK Cloud Audit & Remediation Workflow."
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("GOOGLE_CLOUD_PROJECT", "YOUR_PROJECT_ID"),
        help="Target Google Cloud project ID (defaults to GOOGLE_CLOUD_PROJECT).",
    )
    parser.add_argument(
        "--zone",
        default=os.environ.get("GOOGLE_CLOUD_ZONE", "us-central1-a"),
        help="Target Compute Engine zone to audit (default: us-central1-a).",
    )
    parser.add_argument(
        "--prompt",
        default="Audit Compute Engine VMs in our default zone and remediate any non-running instances.",
        help="Initial workflow trigger message.",
    )
    args = parser.parse_args()

    if args.project in ("YOUR_PROJECT_ID", "your-project-id", ""):
        raise SystemExit(
            "Error: Please set GOOGLE_CLOUD_PROJECT or pass --project=your-project-id"
        )

    os.environ["GOOGLE_CLOUD_PROJECT"] = args.project
    os.environ["GOOGLE_CLOUD_ZONE"] = args.zone
    os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
    os.environ.setdefault("ADK_MODEL", "gemini-3-flash-preview")

    from google.adk.apps import App
    from google.adk.runners import InMemoryRunner
    from google.genai import types
    from app.agent import gcloud_mcp_tools, root_agent

    app = App(name="app", root_agent=root_agent)
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name="app", user_id="sre_operator"
    )

    print("=" * 78)
    print("Starting ADK Workflow: cloud_audit_remediation_workflow")
    print(f"Target Project : {args.project}")
    print(f"Target Zone    : {args.zone}")
    print(f"Trigger Prompt : {args.prompt}")
    print("=" * 78)

    try:
        async for event in runner.run_async(
            user_id="sre_operator",
            session_id=session.id,
            new_message=types.Content(
                role="user", parts=[types.Part.from_text(text=args.prompt)]
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
        print("\n" + "=" * 78)
        print("Final Workflow Session State (`session.state`):")
        print("=" * 78)
        print(
            json.dumps(
                dict(final_session.state) if final_session else {}, indent=2
            )
        )
    finally:
        await gcloud_mcp_tools.close()


if __name__ == "__main__":
    asyncio.run(async_main())
