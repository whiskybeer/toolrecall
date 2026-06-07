import os
import json
import sqlite3
from typing import List, Dict

def _get_db():
    import os
    from toolrecall.config import load_config
    cfg = load_config()
    db_path = str(cfg.get("paths", "cache_db", default="~/.toolrecall/cache.db"))
    db_path = os.path.expandvars(os.path.expanduser(db_path))
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def export_trajectories(output_path: str, limit: int = 1000):
    """
    Export tool calls from the local cache into a fine-tuning dataset (JSONL).
    Format follows OpenAI / Anthropic tool use trajectory standards.
    """
    conn = _get_db()
    
    # We export terminal cache and MCP cache as trajectories
    # File cache is mostly static reads, but terminal/MCP show 'actions'
    
    records: List[Dict] = []
    
    # 1. Export MCP Calls
    mcp_rows = conn.execute(
        "SELECT request_hash, data FROM mcp_cache ORDER BY ROWID DESC LIMIT ?",
        (limit,)
    ).fetchall()
    
    for row in mcp_rows:
        try:
            payload = json.loads(row["data"])
            # ToolRecall stores raw json string in data for MCP
            records.append({
                "type": "mcp_tool_call",
                "hash": row["request_hash"],
                "result": payload
            })
        except:
            pass

    # 2. Export Terminal Calls
    term_rows = conn.execute(
        "SELECT command, output FROM terminal_cache ORDER BY ROWID DESC LIMIT ?",
        (limit,)
    ).fetchall()
    
    for row in term_rows:
        records.append({
            "type": "terminal_execution",
            "command": row["command"],
            "stdout": row["output"]
        })

    # Write to JSONL
    out_file = os.path.expanduser(output_path)
    with open(out_file, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
            
    return len(records), out_file
