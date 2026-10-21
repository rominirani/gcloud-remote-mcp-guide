# Example 01: Wire-Protocol Smoke Test (`curl` & Python)

This example tests the **Google Cloud CLI Remote MCP Server** (`https://cloudcli.googleapis.com/mcp`) directly over HTTPS using the stateless MCP `2026-07-28` JSON-RPC 2.0 protocol.

## What This Covers

1. **Tool Discovery (`tools/list`)**: Unauthenticated discovery of `run_gcloud_command` and `run_bq_command`.
2. **`run_gcloud_command`**: Authenticated Compute Engine region listing (`--limit=3 --format=json --quiet`).
3. **`run_gcloud_command` with `input_files`**: Materializing a virtual configuration file inside the stateless remote sandbox.
4. **`run_bq_command`**: Authenticated BigQuery dataset inspection (`bq ls --datasets=true`).

## Prerequisites

1. Enable the Cloud CLI Execution API on your project:
   ```bash
   export GOOGLE_CLOUD_PROJECT="YOUR_PROJECT_ID"
   gcloud services enable cloudcli.googleapis.com --project="${GOOGLE_CLOUD_PROJECT}" --quiet
   ```
2. Ensure your caller identity holds `roles/mcp.toolUser` (`mcp.tools.call` permission) and least-privilege IAM roles (such as `roles/viewer`).

## Running the Smoke Test

```bash
python3 mcp_probe.py --project YOUR_PROJECT_ID
```

Or run the raw `curl` script:

```bash
export GOOGLE_CLOUD_PROJECT="YOUR_PROJECT_ID"
bash curl_examples.sh
```
