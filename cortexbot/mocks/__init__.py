"""
cortexbot/mocks/__init__.py

Drop-in mock layer for zero-cost local development.
Set USE_MOCKS=true in .env.local to run the full pipeline
without any paid API calls (DAT, Bland AI, Twilio, AWS, DocuSign).
"""
import os

MOCKS_ENABLED = os.getenv("USE_MOCKS", "false").lower() == "true"
