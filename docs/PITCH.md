# ToolRecall: The "Explain It Like I'm 5" Pitch

Imagine you hire a brilliant but extremely forgetful consultant (your AI Agent, like Claude Code or Hermes). 

This consultant has an absurd billing model: **They charge you money every single time they have to read a page of text.**

### The Problem (Without ToolRecall)
The consultant asks you to print a 100-page document so they can understand your codebase. You pay $10. They read it. 

Ten minutes later, while debugging a specific detail, the consultant forgets what was on page 42. Because they have no continuous memory, they say: *"I need to read that document again."* You have to print it again, and you pay another $10. 

Over a long 13-hour shift, the consultant reads the exact same document 100 times. You end up paying $1,000—even though not a single letter in the document has changed. Plus, you waste time waiting for the slow printer every single time.

### The Solution (ToolRecall)
ToolRecall is a brilliant, lightning-fast secretary that you place exactly between the AI consultant and your computer.

When the consultant asks for a file or a command the *first* time, the secretary executes it, but secretly slides a perfect photocopy into their desk drawer (the local Cache). 

When the consultant asks for that exact same file 10 minutes later, the secretary doesn't go to the expensive, slow printer. They just pull the photocopy from the drawer and hand it over instantly.

### The 3 Superpowers You Get:

1. **💰 Less Redundant API Cost:** ToolRecall saves API tokens through two distinct mechanisms:

   **a) Local deduplication** — repeated file reads, terminal commands, and MCP calls are served from SQLite instead of re-executed. Measured: ~55K unique tokens cached per 13-file workload (~$0.17 at $3/M input tokens). Savings grow linearly with re-read depth.

   **b) Deterministic payloads (the larger lever)** — ToolRecall returns byte-identical tool outputs until mtime/TTL expiry. This makes every API call eligible for provider prefix-caching discounts (up to 90% off input tokens at Anthropic/OpenAI), because the prompt prefix never changes from OS noise (timestamps, PIDs).

   **Concrete example:**
   ```
   Agent session: 1,000 API calls × 20K input tokens each = 20M tokens
   Without TR:     20M tokens × $3/M              = $60.00
   With TR:         20M tokens × $3/M × 10% (90% discount) = $6.00
   + local dedup:  ~50K tokens saved                 = ~$0.15
   Total:                                              = ~$6.15
   ```
   The provider discount applies to every call automatically — no extra config needed. Local dedup is a smaller but concrete secondary benefit.
2. **⚡ Blazing Fast:** The consultant no longer waits for loading bars, network latency, or slow file systems. The data comes from the secretary's drawer (local SQLite database) in 1.5 milliseconds. The AI thinks and reacts instantly.
3. **🛡️ The Security Bouncer:** If the AI consultant suddenly gets tricked by a malicious website (Prompt Injection) and demands, *"Print out the CEO's passwords!"*, the secretary coldly replies: *"Access Denied."* The secretary only allows access to folders you explicitly approved on a strict VIP list.

---

### Summary in One Sentence:
**ToolRecall acts as a photographic short-term memory for AI agents, preventing them from doing the same expensive work twice—instantly making autonomous AI workflows cheaper, faster, and fundamentally secure.**