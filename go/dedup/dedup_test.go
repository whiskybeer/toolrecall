package dedup

import (
	"reflect"
	"strings"
	"testing"
)

// fileA / fileB mirror the Python self-test sizes (each ~1.9K chars) so they
// clear min_chars=800 and trigger dedup.
var fileA = strings.Repeat("def cache_lookup(key):\n    ...\n", 60)
var fileB = strings.Repeat("SELECT * FROM turn_log;\n", 80)

// convo returns the exact fixture from the Python self-test
// (toolrecall/adapters/litellm.py __main__) so behaviour parity is direct.
func convo() []Message {
	return []Message{
		{"role": "system", "content": "You are a code reviewer."},
		{"role": "user", "content": "Review cache.py"},
		{"role": "assistant", "content": nil, "tool_calls": []interface{}{map[string]interface{}{"id": "1"}}},
		{"role": "tool", "content": fileA}, // first A -> kept
		{"role": "assistant", "content": "Looks fine. Reading schema."},
		{"role": "tool", "content": []interface{}{map[string]interface{}{"type": "text", "text": fileB}}}, // first B -> kept
		{"role": "user", "content": "Re-read both files."},
		{"role": "tool", "content": fileA}, // dup -> stub
		{"role": "tool", "content": []interface{}{map[string]interface{}{"type": "text", "text": fileB}}}, // dup -> stub
		{"role": "assistant", "content": "Re-read done."},
		{"role": "tool", "content": fileA}, // dup but inside protect_last=2 -> kept
		{"role": "user", "content": "Summarize."},
	}
}

func TestDeduplicateMatchesPythonSelfTest(t *testing.T) {
	opts := DefaultOptions()
	out, stats := DeduplicateMessages(convo(), opts)

	if stats.Blocks != 2 {
		t.Fatalf("stats.Blocks = %d, want 2", stats.Blocks)
	}
	// First occurrences kept in full.
	if got := out[3]["content"]; got != fileA {
		t.Fatalf("out[3] content changed, first A must be kept in full")
	}
	if got := out[5]["content"].([]interface{})[0].(map[string]interface{})["text"]; got != fileB {
		t.Fatalf("out[5] first B must be kept in full")
	}
	// Dups stubbed, pointing at first occurrence index.
	if got := out[7]["content"].(string); !strings.HasPrefix(got, "[toolrecall-dedup]") {
		t.Fatalf("out[7] not stubbed: %q", got)
	} else if !strings.Contains(got, "message 3") {
		t.Fatalf("out[7] stub must reference message 3, got: %q", got)
	}
	got8 := out[8]["content"].([]interface{})[0].(map[string]interface{})["text"].(string)
	if !strings.HasPrefix(got8, "[toolrecall-dedup]") {
		t.Fatalf("out[8] not stubbed: %q", got8)
	} else if !strings.Contains(got8, "message 5") {
		t.Fatalf("out[8] stub must reference message 5, got: %q", got8)
	}
	// Protected tail kept.
	if got := out[10]["content"]; got != fileA {
		t.Fatalf("out[10] protected tail must keep A in full, got: %v", got)
	}
	// Input not mutated.
	if got := convo()[7]["content"]; got != fileA {
		t.Fatalf("input must not be mutated (out[7] index maps to convo[7] = A)")
	}
}

func TestDeterministic(t *testing.T) {
	opts := DefaultOptions()
	out1, s1 := DeduplicateMessages(convo(), opts)
	out2, s2 := DeduplicateMessages(convo(), opts)
	if !reflect.DeepEqual(out1, out2) || s1 != s2 {
		t.Fatalf("determinism violated: out1 != out2 or stats differ")
	}
}

func TestPrefixStability(t *testing.T) {
	// Appending new messages must not change earlier bytes.
	opts := DefaultOptions()
	base := convo()
	outBase, _ := DeduplicateMessages(base, opts)

	longer := append(base,
		Message{"role": "assistant", "content": "One more look."},
		Message{"role": "tool", "content": fileB},
		Message{"role": "user", "content": "Done?"},
	)
	outLonger, _ := DeduplicateMessages(longer, opts)

	if !reflect.DeepEqual(outLonger[:10], outBase[:10]) {
		t.Fatalf("prefix stability violated: appending changed earlier bytes")
	}
	// message[10] (A dup) was protected before, is stubbable now -> allowed to change.
	if got := outLonger[10]["content"].(string); !strings.HasPrefix(got, "[toolrecall-dedup]") {
		t.Fatalf("outLonger[10] should now be stubbable, got: %q", got)
	}
	// New B dup inside protect_last=2 -> kept.
	if got := outLonger[13]["content"]; got != fileB {
		t.Fatalf("outLonger[13] protected tail must keep B, got: %v", got)
	}
}

func TestProtectLastZero(t *testing.T) {
	// protect_last=0 is the append-invariant / prefix-stable mode.
	opts := DefaultOptions()
	opts.ProtectLast = 0
	out, stats := DeduplicateMessages(convo(), opts)
	if stats.Blocks != 3 { // now index 10 (A dup) is also stubbable
		t.Fatalf("protect_last=0 stats.Blocks = %d, want 3", stats.Blocks)
	}
	if got := out[10]["content"].(string); !strings.HasPrefix(got, "[toolrecall-dedup]") {
		t.Fatalf("protect_last=0 should stub tail dup, got: %q", got)
	}
}

func TestShortBlocksNeverStubbed(t *testing.T) {
	opts := DefaultOptions()
	opts.MinChars = 800
	msgs := []Message{
		{"role": "user", "content": "hi"},
		{"role": "tool", "content": "hi"},
	}
	out, stats := DeduplicateMessages(msgs, opts)
	if stats.Blocks != 0 {
		t.Fatalf("short blocks must not be stubbed, Blocks=%d", stats.Blocks)
	}
	if out[1]["content"] != "hi" {
		t.Fatalf("short duplicate content changed")
	}
}

func TestSystemNeverStubbedButRegistered(t *testing.T) {
	opts := DefaultOptions()
	opts.MinChars = 1
	opts.ProtectLast = 0 // so both messages are stubbable-eligible
	blk := strings.Repeat("x", 500) // stub (~130 chars) must be shorter than the block
	msgs := []Message{
		{"role": "system", "content": blk},
		{"role": "user", "content": blk}, // duplicate of system, but user IS stubbable
	}
	out, _ := DeduplicateMessages(msgs, opts)
	// system registered; user dup stubbed
	if got := out[1]["content"].(string); !strings.HasPrefix(got, "[toolrecall-dedup]") {
		t.Fatalf("user duplicate of system content should be stubbed, got %q", got)
	}
	if got := out[0]["content"]; got != blk {
		t.Fatalf("system message must never be modified")
	}
}

func TestEmptyAndEdgeCases(t *testing.T) {
	if out, _ := DeduplicateMessages(nil, DefaultOptions()); len(out) != 0 {
		t.Fatalf("nil input should yield empty output")
	}
	// Non-string / non-list content passes through untouched.
	msgs := []Message{
		{"role": "tool", "content": 42},
		{"role": "tool", "content": nil},
	}
	out, stats := DeduplicateMessages(msgs, DefaultOptions())
	if stats.Blocks != 0 {
		t.Fatalf("non-string content should not stub, Blocks=%d", stats.Blocks)
	}
	if !reflect.DeepEqual(out, msgs) {
		t.Fatalf("non-string content must pass through untouched")
	}
}