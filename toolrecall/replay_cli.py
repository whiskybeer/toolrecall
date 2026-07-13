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

Examples:
  toolrecall replay record my-test
  toolrecall replay replay my-test
  toolrecall replay export my-test > tests/fixtures/my-test.json
  toolrecall replay import tests/fixtures/my-test.json
  toolrecall replay stop
  toolrecall replay list
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
        yn = input(f"Scenario '{data['scenario_name']}' already exists. Overwrite? [y/N] ").strip().lower()
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


def time_str(ts: float) -> str:
    """Format a Unix timestamp to a readable string."""
    import datetime
    dt = datetime.datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M:%S")