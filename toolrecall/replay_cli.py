"""Replay Mode CLI — toolrecall replay <subcommand>

Record and replay agent tool call scenarios for deterministic testing.

Usage:
    toolrecall replay record <scenario_name>   # Start recording mode
    toolrecall replay replay <scenario_name>   # Start replay mode
    toolrecall replay stop                     # Stop Replay mode
    toolrecall replay status                   # Show current Replay mode
    toolrecall replay list                     # List recorded scenarios
    toolrecall replay show <scenario_name>     # Show recorded calls in a scenario
    toolrecall replay export <scenario_name>   # Export scenario as JSON
    toolrecall replay import <file.json>       # Import scenario from JSON
    toolrecall replay delete <scenario_name>   # Delete a scenario
    toolrecall replay tree [<tree_id>|new <name>]  # List/show/create fork trees
    toolrecall replay fork <tree> <node> <name>    # Fork a branch from a node
    toolrecall replay at <tree> <node>             # Show a node's inherited call chain
"""

import json
import os


def cmd_replay(args: list[str]) -> None:
    """Dispatch replay subcommands."""
    if not args:
        _print_usage()
        return

    sub = args[0]
    rest = args[1:]

    if sub == "record":
        _cmd_record(rest)
    elif sub == "replay":
        _cmd_replay(rest)
    elif sub == "stop":
        _cmd_stop()
    elif sub == "status":
        _cmd_status()
    elif sub == "list":
        _cmd_list()
    elif sub == "show":
        _cmd_show(rest)
    elif sub == "export":
        _cmd_export(rest)
    elif sub == "import":
        _cmd_import(rest)
    elif sub == "delete":
        _cmd_delete(rest)
    elif sub == "tree":
        _cmd_tree(rest)
    elif sub == "fork":
        _cmd_fork(rest)
    elif sub == "at":
        _cmd_at(rest)
    elif sub in ("-h", "--help", "help"):
        _print_usage()
    else:
        print(f"Unknown replay subcommand: {sub}")
        _print_usage()


def _print_usage():
    print("""Usage: toolrecall replay <subcommand> [args]

Record and replay agent tool call scenarios for deterministic testing.

Subcommands:
  record <scenario>    Start recording mode — all tool calls are recorded
  replay <scenario>    Start replay mode — matching calls return cached responses
  stop                 Stop Replay mode (both recording and replaying)
  status               Show current Replay mode status
  list                 List all recorded scenarios
  show <scenario>      Show recorded calls in a scenario
  export <scenario>    Export scenario as JSON (portable, git-committable)
  import <file.json>   Import scenario from exported JSON file
  delete <scenario>    Delete a scenario
  tree                 List replay trees
  tree <tree_id>       Show nodes of a tree
  tree new <name>      Create a replay tree (prints tree_id + root node)
  fork <tree> <node> <name>  Fork a branch from a node
  at <tree> <node>     Show the call chain a node inherits (root first)

Examples:
  toolrecall replay record my-test
  toolrecall replay replay my-test
  toolrecall replay export my-test > tests/fixtures/my-test.json
  toolrecall replay import tests/fixtures/my-test.json
  toolrecall replay stop
  toolrecall replay list
  toolrecall replay tree new sweep1
  toolrecall replay fork <tree_id> <root_node> branch-A
  toolrecall replay at <tree_id> <node_id>
""")


def _cmd_record(rest: list[str]) -> None:
    if not rest:
        print("Error: Missing scenario name. Usage: toolrecall replay record <name>")
        return
    scenario = rest[0]
    print("⚠ Replay recording is not yet wired into the daemon.")
    print("   This command stores no tool-call data currently.")
    print("   See: https://github.com/whiskybeer/toolrecall/issues (planned feature)")
    print(f"   Scenario name saved: {scenario} (for future use)")


def _cmd_replay(rest: list[str]) -> None:
    if not rest:
        print("Error: Missing scenario name. Usage: toolrecall replay replay <name>")
        return
    scenario = rest[0]
    from toolrecall.replay import start_replay, ReplayManager

    # Verify scenario exists
    replay = ReplayManager()
    scenarios = replay.list_scenarios()
    names = [s["name"] for s in scenarios]
    if scenario not in names:
        print(f"Warning: Scenario '{scenario}' not found.")
        print(f"   Available scenarios: {', '.join(names) if names else '(none)'}")
        yn = input("  Start replay anyway? [y/N] ").strip().lower()
        if yn != "y":
            return
    result = start_replay(scenario)  # noqa: F841 — intentional discard
    print(f"🟢 Replay started: {scenario}")
    print("   Matching tool calls will be served from recorded responses.")
    print("   Run 'toolrecall replay stop' to stop replay.")


def _cmd_stop() -> None:
    from toolrecall.replay import stop

    result = stop()
    if result["replay_mode"] == "stopped":
        print(f"🔴 Replay stopped (was: {result['was']}, scenario: {result['scenario']})")
    else:
        print("Replay mode was not active.")


def _cmd_status() -> None:
    from toolrecall.replay import status

    s = status()
    print(f"Replay Mode: {s['replay_mode']}")
    print(f"Scenario: {s['scenario'] or '(none)'}")
    print(f"Active:   {s['is_active']}")
    if s["is_recording"]:
        print("  → Recording: all tool calls are being saved")
    elif s["is_replaying"]:
        print("  → Replaying: matching calls return cached responses")


def _cmd_list() -> None:
    from toolrecall.replay import ReplayManager

    replay = ReplayManager()
    scenarios = replay.list_scenarios()
    if not scenarios:
        print("No Replay scenarios recorded.")
        return
    print(f"{'Scenario':<30} {'Calls':<8} {'Last recorded':<20}")
    print("-" * 60)
    for s in scenarios:
        last = s["last_recorded"]
        last_str = time_str(last) if last else "never"
        print(f"{s['name']:<30} {s['call_count']:<8} {last_str:<20}")


def _cmd_show(rest: list[str]) -> None:
    if not rest:
        print("Error: Missing scenario name. Usage: toolrecall replay show <name>")
        return
    from toolrecall.replay import ReplayManager

    replay = ReplayManager()
    calls = replay.get_scenario(rest[0])
    if not calls:
        print(f"No calls in scenario '{rest[0]}'.")
        return
    print(f"Scenario: {rest[0]} ({len(calls)} calls)")
    print("-" * 60)
    for c in calls:
        print(f"  [{c['call_index']}] {c['tool_name']}({json.dumps(c['args'])[:80]})")
        resp_preview = json.dumps(c["response"])
        if len(resp_preview) > 120:
            resp_preview = resp_preview[:120] + "..."
        print(f"      → {resp_preview}")
        print()


def _cmd_export(rest: list[str]) -> None:
    if not rest:
        print("Error: Missing scenario name. Usage: toolrecall replay export <name>")
        return
    scenario = rest[0]
    from toolrecall.replay import ReplayManager

    replay = ReplayManager()
    data = replay.export_scenario(scenario)
    print(json.dumps(data, indent=2))


def _cmd_import(rest: list[str]) -> None:
    if not rest:
        print("Error: Missing file path. Usage: toolrecall replay import <file.json>")
        return
    filepath = rest[0]
    if not os.path.exists(filepath):
        print(f"Error: File not found: {filepath}")
        return
    with open(filepath) as f:
        data = json.load(f)
    from toolrecall.replay import ReplayManager

    replay = ReplayManager()
    # Check if exists
    existing = [s["name"] for s in replay.list_scenarios()]
    overwrite = False
    if data.get("scenario_name") in existing:
        yn = (
            input(f"Scenario '{data['scenario_name']}' already exists. Overwrite? [y/N] ")
            .strip()
            .lower()
        )
        if yn != "y":
            print("Import cancelled.")
            return
        overwrite = True
    result = replay.import_scenario(data, overwrite=overwrite)
    print(f"✅ Imported {result['calls_imported']} calls to scenario '{result['scenario_name']}'.")


def _cmd_delete(rest: list[str]) -> None:
    if not rest:
        print("Error: Missing scenario name. Usage: toolrecall replay delete <name>")
        return
    scenario = rest[0]
    yn = input(f"Delete scenario '{scenario}'? [y/N] ").strip().lower()
    if yn != "y":
        print("Cancelled.")
        return
    from toolrecall.replay import ReplayManager

    replay = ReplayManager()
    count = replay.delete_scenario(scenario)
    print(f"Deleted scenario '{scenario}' ({count} calls removed).")


# ─── Tree / fork subcommands (G1) ──────────────────────────────────────────────


def _cmd_tree(rest: list[str]) -> None:
    """tree                       — list trees
    tree <tree_id>              — show nodes of a tree
    tree new <name>             — create a tree (prints tree_id and root node)"""
    from toolrecall.replay import ReplayManager

    replay = ReplayManager()
    if not rest:
        trees = replay.list_trees()
        if not trees:
            print("No replay trees. Create one: toolrecall replay tree new <name>")
            return
        print(f"{'TREE ID':<18} {'NODES':>6} {'CALLS':>6}  CREATED")
        for t in trees:
            print(
                f"{t['tree_id']:<18} {t['nodes']:>6} {t['calls']:>6}  {time_str(t['created_at'])}"
            )
        return
    if rest[0] == "new":
        if len(rest) < 2:
            print("Error: Missing tree name. Usage: toolrecall replay tree new <name>")
            return
        tree = replay.fork_tree(rest[1])
        print(f"Tree created: {tree['tree_id']}")
        print(f"  root node: {tree['root']}")
        return
    tid = rest[0]
    try:
        tree = replay.get_tree(tid)
    except ValueError as e:
        print(f"Error: {e}")
        return
    print(f"Tree {tid} — {len(tree['nodes'])} nodes (newest first):")
    for n in tree["nodes"]:
        parent = n["parent_id"] or "—"
        print(
            f"  {n['node_id']}  depth {n['depth']:<3} parent {parent}  "
            f"{n['branch_name']}  ({time_str(n['created_at'])})"
        )


def _cmd_fork(rest: list[str]) -> None:
    """fork <tree_id> <parent_node_id> <branch_name> — fork a branch node"""
    if len(rest) < 3:
        print("Usage: toolrecall replay fork <tree_id> <parent_node_id> <branch_name>")
        return
    from toolrecall.replay import ReplayManager

    replay = ReplayManager()
    try:
        node = replay.fork_node(rest[0], rest[1], rest[2])
    except ValueError as e:
        print(f"Error: {e}")
        return
    print(
        f"Branch '{rest[2]}' forked from {rest[1]}: node {node['node_id']} (depth {node['depth']})"
    )


def _cmd_at(rest: list[str]) -> None:
    """at <tree_id> <node_id>    — show the call chain inherited by a node"""
    if len(rest) < 2:
        print("Usage: toolrecall replay at <tree_id> <node_id>")
        return
    from toolrecall.replay import ReplayManager

    replay = ReplayManager()
    chain = replay.list_nodes(rest[0], rest[1])
    if not chain:
        print(f"Error: Unknown node {rest[1]!r} in tree {rest[0]!r}")
        return
    print(f"Node {rest[1]} chain (root first, deepest = replay view):")
    for n in chain:
        own = replay.node_call_count(rest[0], n["node_id"])
        marker = " ←" if n["node_id"] == rest[1] else ""
        print(
            f"  depth {n['depth']:<3} {n['node_id']}  {n['branch_name']:<20} {own} own calls{marker}"
        )


def time_str(ts: float) -> str:
    """Format a Unix timestamp to a readable string."""
    import datetime

    dt = datetime.datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M:%S")
