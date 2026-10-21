import os

from dotenv import load_dotenv

# Finds the .env at the repo root so passwords never appear in test code.
# override=True: .env wins over stale variables left in the PowerShell session.
load_dotenv(override=True)

# langsmith came in with langgraph and is also a pytest plugin; keep it from sending traces anywhere.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"