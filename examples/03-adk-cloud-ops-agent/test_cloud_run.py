#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Live Verification Script for the Deployed Cloud Run ADK Agent Service."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Any


def get_identity_token() -> str:
    """Retrieves an OIDC identity token from gcloud for authenticated Cloud Run calls."""
    gcloud_bin = shutil.which("gcloud")
    if not gcloud_bin:
        raise RuntimeError("Could not find 'gcloud' on PATH.")

    res = subprocess.run(
        [gcloud_bin, "auth", "print-identity-token", "--quiet"],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return res.stdout.strip()


def http_json(
    method: str, url: str, id_token: str, payload: dict[str, Any] | None = None
) -> tuple[int, Any]:
    """Makes an authenticated HTTP request to the Cloud Run ADK API server."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {id_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        return err.code, {"error": raw}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test the deployed ADK Cloud Ops Agent on Google Cloud Run."
    )
    parser.add_argument("--url", required=True, help="Cloud Run service base URL")
    parser.add_argument(
        "--project",
        default=os.environ.get("GOOGLE_CLOUD_PROJECT", "YOUR_PROJECT_ID"),
        help="Target GCP project ID",
    )
    args = parser.parse_args()

    if args.project in ("YOUR_PROJECT_ID", "your-project-id", ""):
        raise SystemExit(
            "Please set GOOGLE_CLOUD_PROJECT or pass --project=\"${GOOGLE_CLOUD_PROJECT}\"."
        )

    base_url = args.url.rstrip("/")
    id_token = get_identity_token()

    print(f"=== Testing Deployed Cloud Run ADK Service: {base_url} ===\n")

    # 1. GET /list-apps
    status_apps, apps = http_json("GET", f"{base_url}/list-apps", id_token)
    print(f"[1] GET /list-apps -> HTTP {status_apps}: {apps}")

    # 2. POST /apps/app/users/cloudrun_tester/sessions/session_01
    session_url = f"{base_url}/apps/app/users/cloudrun_tester/sessions/session_01"
    status_sess, _ = http_json("POST", session_url, id_token, {})
    print(f"[2] POST {session_url} -> HTTP {status_sess}")

    # 3. POST /run
    prompt = (
        f"List the Compute Engine VM instances in project {args.project} "
        "(limit to 2, JSON format) and report their name, zone, and status."
    )
    print(f"[3] POST /run with prompt: {prompt!r}")
    run_payload = {
        "app_name": "app",
        "user_id": "cloudrun_tester",
        "session_id": "session_01",
        "new_message": {
            "role": "user",
            "parts": [{"text": prompt}],
        },
        "streaming": False,
    }
    status_run, events = http_json(
        "POST", f"{base_url}/run", id_token, run_payload
    )
    print(f"    -> HTTP {status_run}")

    final_text = ""
    if isinstance(events, list):
        for ev in events:
            content = ev.get("content", {})
            for part in content.get("parts", []):
                if "text" in part and part["text"]:
                    final_text = part["text"]

    print(f"\n[Cloud Run Agent Final Response]:\n{final_text}\n")


if __name__ == "__main__":
    main()
