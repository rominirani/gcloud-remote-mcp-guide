# Example 02: Local Machine & IDE Configuration (Antigravity / Gemini CLI)

Connect your local AI coding assistant (Google Antigravity or Gemini CLI) to the **Google Cloud CLI Remote MCP Server** without running a local `gcloud` subprocess.

## Setup Steps

1. **Authenticate Application Default Credentials (ADC)**:
   ```bash
   gcloud auth application-default login
   ```
2. **Add `gcloud-remote` to `~/.gemini/config/mcp_config.json`**:
   Copy [`mcp_config.json`](./mcp_config.json) into `~/.gemini/config/mcp_config.json`:
   ```json
   {
     "mcpServers": {
       "gcloud-remote": {
         "serverUrl": "https://cloudcli.googleapis.com/mcp",
         "authProviderType": "google_credentials"
       }
     }
   }
   ```

> **Important**: `"authProviderType": "google_credentials"` is mandatory so the MCP client attaches your ADC OAuth 2.0 Bearer token with the `https://www.googleapis.com/auth/cloud-platform` scope.

## Sample Prompts to Try

### 1. Cloud Logging Diagnostics (`run_gcloud_command`)
> *"Using the `gcloud` remote MCP server in project `YOUR_PROJECT_ID`, fetch the 5 most recent error-level log entries across the project in JSON format and summarize them."*

### 2. Serverless Config Deployment with `input_files` (`run_gcloud_command`)
> *"Using `run_gcloud_command` with `input_files` in project `YOUR_PROJECT_ID`, validate or deploy a Cloud Run service manifest named `hello-mcp` in `us-central1`."*

### 3. BigQuery Dataset & Job Inspection (`run_bq_command`)
> *"Using `run_bq_command` in project `YOUR_PROJECT_ID`, list all BigQuery datasets and check recent jobs."*
