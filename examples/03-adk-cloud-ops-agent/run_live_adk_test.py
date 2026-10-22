#!/usr/bin/env -S uv run
"""Live End-to-End ADK Agent Verification against Google Cloud CLI Remote MCP.

Executes two live prompts through ADK's InMemoryRunner using gemini-3-flash-preview
and McpToolset connected to https://cloudcli.googleapis.com/mcp:
  1. Compute Engine VM Inventory via run_gcloud_command
  2. BigQuery Dataset Inventory via run_bq_command
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "YOUR_PROJECT_ID")
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
os.environ["ADK_MODEL"] = "gemini-3-flash-preview"


async def run_prompt(runner: Any, user_id: str, session_id: str, prompt: str) -> dict[str, Any]:
    """Runs a single user prompt through the ADK runner and records tool calls."""
    from google.genai import types

    print(f"\n--- Prompt: {prompt} ---")
    tool_calls: list[dict[str, Any]] = []
    final_text = ""

    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text=prompt)]
        ),
    ):
        for fn_call in event.get_function_calls() or []:
            print(f"  [Tool Call] {fn_call.name}({json.dumps(fn_call.args)})")
            tool_calls.append({"name": fn_call.name, "args": dict(fn_call.args)})

        if event.is_final_response() and event.content and event.content.parts:
            texts = [p.text for p in event.content.parts if getattr(p, "text", None)]
            if texts:
                final_text = "\n".join(texts)

    print(f"  [Final Answer]:\n{final_text}\n")
    return {
        "prompt": prompt,
        "tool_calls": tool_calls,
        "final_answer": final_text,
    }


async def main() -> None:
    if PROJECT_ID == "YOUR_PROJECT_ID":
        raise SystemExit(
            "Please set GOOGLE_CLOUD_PROJECT before running:\n"
            "  export GOOGLE_CLOUD_PROJECT=\"YOUR_PROJECT_ID\""
        )

    from google.adk.runners import InMemoryRunner
    from app.agent import gcloud_mcp_tools, root_agent

    runner = InMemoryRunner(agent=root_agent, app_name="cloud_ops_app")
    session = await runner.session_service.create_session(
        app_name="cloud_ops_app", user_id="test_user"
    )

    prompts = [
        (
            f"List the Compute Engine VM instances in project {PROJECT_ID} "
            "(limit to 2, JSON format) and tell me their names, machine types, and status."
        ),
        (
            f"Using run_bq_command, list all BigQuery datasets in project {PROJECT_ID} "
            "in prettyjson format and tell me how many datasets are in each location."
        ),
    ]

    for prompt in prompts:
        await run_prompt(runner, "test_user", session.id, prompt)

    await gcloud_mcp_tools.close()


if __name__ == "__main__":
    asyncio.run(main())
