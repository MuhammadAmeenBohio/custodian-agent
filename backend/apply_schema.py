"""Apply backend/schema.sql as agent_user (the owner of the agent schema). Dev only: it drops and recreates."""
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv


def main() -> None:
    load_dotenv(override=True)
    with psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "15432")),
        dbname=os.environ["POSTGRES_DB"],
        user="agent_user",
        password=os.environ["AGENT_DB_PASSWORD"],
    ) as conn:
        conn.execute((Path(__file__).parent / "schema.sql").read_text(encoding="utf-8"))
        n = conn.execute("SELECT count(*) FROM agent.runs").fetchone()[0]
    print(f"backend schema applied: {n} runs")


if __name__ == "__main__":
    main()
