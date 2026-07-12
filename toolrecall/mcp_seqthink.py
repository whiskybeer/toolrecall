"""ToolRecall Sequential Thinking MCP Server — stdlib-only.
Optional replacement for `npx -y @modelcontextprotocol/server-sequential-thinking`.
Pure logic: validates reasoning steps, detects contradictions, tracks depth.
Zero network calls, zero dependencies.
"""
import json
import sys
import re

TOOLS = [
    {"name": "think_step", "description": "Process a single reasoning step",
     "inputSchema": {"type": "object", "properties": {
         "thought": {"type": "string", "description": "Current reasoning step"},
         "step_number": {"type": "integer"},
         "previous_thoughts": {"type": "array", "items": {"type": "string"}},
         "branch_id": {"type": "string", "description": "Optional branch identifier"}},
         "required": ["thought", "step_number"]}},
    {"name": "analyze", "description": "Analyze a reasoning chain for gaps",
     "inputSchema": {"type": "object", "properties": {
         "thoughts": {"type": "array", "items": {"type": "string"}}},
         "required": ["thoughts"]}},
    {"name": "validate_reasoning", "description": "Validate reasoning for contradictions",
     "inputSchema": {"type": "object", "properties": {
         "premises": {"type": "array", "items": {"type": "string"}},
         "conclusion": {"type": "string"}},
         "required": ["premises", "conclusion"]}},
]

def _detect_keywords(thought: str) -> list:
    issues = []
    if re.search(r'\b(maybe|perhaps|possibly|not sure|unclear)\b', thought, re.I):
        issues.append("Low confidence: contains hedging language")
    if "?" in thought:
        issues.append("Open question: step ends with a question")
    if len(thought.split()) < 5:
        issues.append("Very short step: may lack substance")
    return issues

def _check_contradictions(thoughts: list) -> list:
    contradictions = []
    for i, t1 in enumerate(thoughts):
        for j, t2 in enumerate(thoughts):
            if i >= j:
                continue
            # Simple negation check
            for word in ["not ", "cannot ", "doesn't ", "isn't ", "won't "]:
                if word in t1.lower() and word not in t2.lower():
                    # Extract core noun phrase
                    t1_core = t1.lower().replace(word, "¬")
                    t2_core = t2.lower()
                    # If same core appears negated in one but not other
                    for w in t1_core.split():
                        w = w.strip(".,!?:;")
                        if len(w) > 3 and w in t2_core and w not in ("this", "that", "the", "and", "but", "not"):
                            contradictions.append(f"Step {i+1} vs step {j+1}: '{w}' appears negated in one")
                            break
    return contradictions

def _handle(method, params):
    if method == "think_step":
        thought = params.get("thought", "")
        step = params.get("step_number", 1)
        prev = params.get("previous_thoughts", [])
        branch = params.get("branch_id", "main")
        issues = _detect_keywords(thought)
        cont = _check_contradictions(prev + [thought])
        return {
            "step": step, "branch": branch, "depth": len(prev) + 1,
            "analyzed": True, "issues": issues, "contradictions": cont,
        }
    elif method == "analyze":
        thoughts = params.get("thoughts", [])
        cont = _check_contradictions(thoughts)
        gaps = []
        for i, t in enumerate(thoughts):
            issues = _detect_keywords(t)
            if issues:
                gaps.append({"step": i+1, "issues": issues})
        return {
            "total_steps": len(thoughts),
            "contradictions": cont,
            "gaps": gaps,
            "coherent": len(cont) == 0 and len(gaps) == 0,
        }
    elif method == "validate_reasoning":
        premises = params.get("premises", [])
        conclusion = params.get("conclusion", "")
        cont = _check_contradictions(premises + [conclusion])
        # Check if conclusion follows from premises (heuristic)
        premise_keywords = set()
        for p in premises:
            for w in p.lower().split():
                w = w.strip(".,!?:;")
                if len(w) > 4:
                    premise_keywords.add(w)
        conc_words = set(w.strip(".,!?:;") for w in conclusion.lower().split() if len(w) > 4)
        supported = len(conc_words & premise_keywords) / max(len(conc_words), 1) if conc_words else 1.0
        return {
            "valid": supported > 0.3,
            "contradictions": cont,
            "premises_used": len(premises),
            "conclusion_supported": f"{supported:.0%}",
            "verdict": "Supported" if supported > 0.3 else "Not fully supported by premises",
        }
    return None

def main():
    sys.stderr.write("ToolRecall Sequential Thinking MCP (Python stdlib, zero deps)\n")
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid, method, params = req.get("id", 0), req.get("method", ""), req.get("params", {})
        resp = {"jsonrpc": "2.0", "id": rid}
        if method == "initialize":
            resp["result"] = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "toolrecall-seqthink", "version": "0.1.0"}}
        elif method == "tools/list":
            resp["result"] = {"tools": TOOLS}
        elif method == "tools/call":
            result = _handle(params.get("name", ""), params.get("arguments", {}))
            if result is None:
                resp["error"] = {"code": -32601, "message": "Unknown tool"}
            else:
                resp["result"] = {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}
        elif method in ("notifications/initialized", "close"):
            continue
        else:
            resp["error"] = {"code": -32601, "message": f"Unknown method: {method}"}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()

if __name__ == "__main__":
    main()