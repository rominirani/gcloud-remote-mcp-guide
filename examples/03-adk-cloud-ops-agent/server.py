"""FastAPI server entrypoint for Cloud Run deployment of the ADK Cloud Ops Agent."""

import os
from google.adk.cli.fast_api import get_fast_api_app

AGENTS_DIR = os.path.dirname(os.path.abspath(__file__))

app = get_fast_api_app(
    agents_dir=AGENTS_DIR,
    web=False,
)
