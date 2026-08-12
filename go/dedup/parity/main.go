// Command parity is a tiny executable that prints the dedup stub strings for
// the canonical fixture, so the Go output can be diffed byte-for-byte against
// the Python self-test (toolrecall/adapters/litellm.py __main__).
package main

import (
	"fmt"

	"github.com/whiskybeer/toolrecall/go-dedup"
)

func main() {
	fileA := repeat("def cache_lookup(key):\n    ...\n", 60)
	fileB := repeat("SELECT * FROM turn_log;\n", 80)

	msgs := []dedup.Message{
		{"role": "system", "content": "You are a code reviewer."},
		{"role": "user", "content": "Review cache.py"},
		{"role": "assistant", "content": nil, "tool_calls": []interface{}{map[string]interface{}{"id": "1"}}},
		{"role": "tool", "content": fileA},
		{"role": "assistant", "content": "Looks fine. Reading schema."},
		{"role": "tool", "content": []interface{}{map[string]interface{}{"type": "text", "text": fileB}}},
		{"role": "user", "content": "Re-read both files."},
		{"role": "tool", "content": fileA},
		{"role": "tool", "content": []interface{}{map[string]interface{}{"type": "text", "text": fileB}}},
		{"role": "assistant", "content": "Re-read done."},
		{"role": "tool", "content": fileA},
		{"role": "user", "content": "Summarize."},
	}

	out, stats := dedup.DeduplicateMessages(msgs, dedup.DefaultOptions())
	fmt.Printf("blocks:%d chars_saved:%d est_tok:%d\n", stats.Blocks, stats.CharsSaved, stats.EstTokensSaved)
	fmt.Printf("GO out7: %q\n", out[7]["content"])
	fmt.Printf("GO out8: %q\n", out[8]["content"].([]interface{})[0].(map[string]interface{})["text"])
}

func repeat(s string, n int) string {
	r := ""
	for i := 0; i < n; i++ {
		r += s
	}
	return r
}