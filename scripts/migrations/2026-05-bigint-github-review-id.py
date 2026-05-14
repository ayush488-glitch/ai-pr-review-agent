"""One-shot ALTER: github_review_id INT -> BIGINT on reviews table."""
import asyncio, os, re
from pathlib import Path

env = Path("/Users/ayushsingh/Desktop/ai-pr-review-agent/.env").read_text()
m = re.search(r'^DATABASE_URL=(.+)$', env, re.M)
url = m.group(1).strip().strip('"').strip("'")

import asyncpg

async def main():
    # Convert sqlalchemy URL to asyncpg-friendly
    pg_url = url.replace("postgresql+asyncpg://", "postgresql://").split("?")[0]
    conn = await asyncpg.connect(pg_url, ssl="require")
    # Find tables that have github_review_id
    rows = await conn.fetch("""
        SELECT table_name, data_type FROM information_schema.columns
        WHERE column_name='github_review_id'
    """)
    for r in rows:
        print("Before:", dict(r))
    for r in rows:
        if r["data_type"] == "integer":
            sql = f'ALTER TABLE {r["table_name"]} ALTER COLUMN github_review_id TYPE BIGINT;'
            print("Running:", sql)
            await conn.execute(sql)
    rows2 = await conn.fetch("""
        SELECT table_name, data_type FROM information_schema.columns
        WHERE column_name='github_review_id'
    """)
    for r in rows2:
        print("After:", dict(r))
    await conn.close()

asyncio.run(main())
