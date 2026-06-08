"""ToolRecall Synthetic Data Export Example

This script demonstrates how to programmatically export the SQLite cache
into a JSONL file. 

The resulting file contains high-fidelity pairs of [Action -> OS State Observation]
which can be used for RLHF (Reinforcement Learning from Human Feedback) 
or DPO (Direct Preference Optimization) to train local L0 models.

Usage:
    python examples/python-dataset-export.py
"""
import os
import sqlite3
import json
from pathlib import Path

def export_trajectories(output_path: str = "agent_trajectories.jsonl"):
    """Export the terminal cache into a training-ready JSONL format."""
    db_path = Path.home() / ".toolrecall" / "cache.db"
    if not db_path.exists():
        print(f"No cache database found at {db_path}")
        return

    print(f"Exporting trajectories from {db_path}...")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT command, output, exit_code FROM terminal_cache")
        rows = cursor.fetchall()
        
        with open(output_path, "w", encoding="utf-8") as f:
            for row in rows:
                trajectory = {
                    "action": {"type": "terminal", "command": row["command"]},
                    "observation": {"output": row["output"], "exit_code": row["exit_code"]}
                }
                f.write(json.dumps(trajectory) + "\n")
        
        print(f"✅ Successfully exported {len(rows)} trajectories to {output_path}")
        print("This data is ready for SFT (Supervised Fine-Tuning) or DPO.")
    
    except sqlite3.OperationalError:
        print("Terminal cache table not found (no commands executed yet).")
    finally:
        conn.close()

if __name__ == "__main__":
    export_trajectories()
