# Real-Agent Debug Loop Benchmark

This page documents the real-agent benchmark referenced in the README.

**Setup:** A Hermes agent fixing bugs in ToolRecall's own code, 10 turns, 5 writes.

**Result:** 36.4% input token savings — 63,326 input tokens without TR → 40,270 with TR.

Write-invalidation resets the cache on every edit, so savings are lower than read-only benchmarks (98%+) but reflect actual edit-heavy sessions. At 50 turns with the same write frequency, estimated savings climb to ~68%.

See [Benchmark](BENCHMARK.md) for the full 13-hour case study.