# Tiger Cloud MCP Setup

This guide wires your Tiger Cloud instance into Claude Desktop (or Cursor) via MCP.
Once connected, the coding agent can introspect your schema, run queries, and verify
migrations live — on camera for the course demo.

## What you get

- Ask Claude: "show me the agent_events hypertable schema"
- Ask Claude: "run the last 10 LLM call events for review X"
- Ask Claude: "verify that the DiskANN index exists on code_chunks"
- Ask Claude: "what is the p95 latency for the security agent in the last hour?"

All of these work from inside a Claude chat — no psql, no dashboard tab.

## Prerequisites

1. Tiger Cloud instance provisioned at tigerdata.com
2. Migration run: `psql $TIGER_DATABASE_URL < scripts/migrations/2026-06-tiger-init.sql`
3. Node.js 18+ installed (for the MCP server)
4. Claude Desktop installed (or Cursor with MCP support)

## Step 1: Install the Postgres MCP server

```bash
npm install -g @modelcontextprotocol/server-postgres
```

Verify:
```bash
mcp-server-postgres --version
```

## Step 2: Configure Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tiger-cloud": {
      "command": "mcp-server-postgres",
      "args": [
        "--connection-string",
        "postgresql://user:pass@host.tigerdata.com:5432/dbname"
      ]
    }
  }
}
```

Replace the connection string with your actual TIGER_DATABASE_URL.

**Security note:** The MCP server gets read+write access to your Tiger instance.
For the demo, this is fine. For production, create a read-only role:

```sql
CREATE ROLE claude_mcp LOGIN PASSWORD 'secure-password';
GRANT CONNECT ON DATABASE your_db TO claude_mcp;
GRANT USAGE ON SCHEMA public TO claude_mcp;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO claude_mcp;
```

Then use that role's DSN in the config.

## Step 3: Configure Cursor (alternative)

In Cursor settings → MCP Servers → Add:

```json
{
  "name": "tiger-cloud",
  "command": "mcp-server-postgres",
  "args": ["--connection-string", "YOUR_TIGER_DATABASE_URL"]
}
```

## Step 4: Restart and verify

1. Quit and reopen Claude Desktop (or Cursor)
2. In a new chat, type: `list tables in the tiger database`
3. You should see: `agent_events`, `code_chunks`, `pr_review_records`, `repo_file_index`, etc.

## Demo queries for the course

These are designed to be run live on camera to show Tiger's capabilities:

```sql
-- 1. Show the hypertable structure (proves TimescaleDB is active)
SELECT * FROM timescaledb_information.hypertables;

-- 2. Show the last 10 LLM calls with cost
SELECT ts, agent, model, tokens_in, tokens_out, cost_usd
FROM agent_events
WHERE event_type = 'llm.call'
ORDER BY ts DESC
LIMIT 10;

-- 3. Live cost dashboard (from continuous aggregate — instant, no scan)
SELECT agent, sum(cost_usd) AS total_cost, max(p95_ms) AS p95_latency_ms
FROM agent_health_1m
WHERE bucket >= now() - INTERVAL '1 hour'
GROUP BY agent
ORDER BY total_cost DESC;

-- 4. Show the DiskANN index (proves pgvectorscale is active)
\d code_chunks
-- or:
SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'code_chunks';

-- 5. Verify freshness decay works
SELECT path, updated_at,
       EXP(-EXTRACT(EPOCH FROM (now() - updated_at)) / 3600.0 / 168.0) AS freshness_score
FROM code_chunks
WHERE repo = 'your-org/your-repo'
ORDER BY freshness_score DESC
LIMIT 10;

-- 6. Show hypertable chunks (time partitioning)
SELECT * FROM timescaledb_information.chunks
WHERE hypertable_name = 'agent_events'
ORDER BY range_start DESC
LIMIT 5;
```

## Troubleshooting

**MCP server not showing in Claude**
- Check JSON syntax in config file
- Ensure `mcp-server-postgres` is in your PATH
- Check Claude Desktop logs: `~/Library/Logs/Claude/`

**Connection refused**
- Verify TIGER_DATABASE_URL is correct
- Check Tiger Cloud firewall rules allow your IP
- Test directly: `psql $TIGER_DATABASE_URL -c 'SELECT 1'`

**pgvectorscale extension missing**
- Tiger Cloud HA image includes pgvectorscale. Local docker: use `timescale/timescaledb-ha:pg16`
- Verify: `SELECT * FROM pg_extension WHERE extname = 'vectorscale';`
