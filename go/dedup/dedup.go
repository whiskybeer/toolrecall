// Copyright (c) 2026 Robin Schultka
// SPDX-License-Identifier: MIT
// Source: https://github.com/whiskybeer/toolrecall
//
// Package dedup is a pure, dependency-free Go port of ToolRecall's
// request-level duplicate-content dedup.
//
// Reference implementation (the spec for this port):
//
//	toolrecall/adapters/litellm.py :: dedup_messages()
//
// It scans a chat-completion messages array for large text blocks that appear
// more than once in the same request — typically repeated file reads / tool
// results in agent loops — and replaces every duplicate AFTER the first
// occurrence with a short stub.
//
// Design properties (mirrored from the Python original):
//
//   - Keep-first, stub-later — earlier messages are never rewritten when a new
//     duplicate appears later, so the byte-prefix of the request stays stable
//     across turns. Provider-side prompt caching keeps hitting.
//   - Deterministic — same messages + same options -> same output, every time.
//   - Fail-open philosophy — this pure function cannot panic on well-typed
//     input; wrapper layers should pass requests through untouched on error.
//   - Stdlib only. No external dependencies. Copy `dedup.go` into any Go
//     product (e.g. a gateway) to vendor the logic under your own review.
//
// Known limitations (identical to the Python original):
//
//   - Matching is whole-block exact (hash of the full string / text part), not
//     substring search. A file embedded inside a longer string is a different
//     block and will not match.
//   - len() counts BYTES (Go strings are UTF-8), the Python original counts
//     code points. For ASCII-dominated file/tool content the min_chars and
//     stub-vs-block length heuristics agree; for heavy multibyte content the
//     byte count is slightly larger. This is a deliberate, documented diff.
package dedup

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
)

// Default stub template. Placeholders mirror the Python original:
//
//	{chars}  byte length of the omitted block
//	{digest} sha256[:16] of the block
//	{index}  message position (0-based) of the first occurrence
const defaultStubTemplate = "[toolrecall-dedup] Duplicate content omitted (%d chars, sha256:%s). " +
	"The byte-identical content already appears in message %d of this request."

// Options controls dedup behaviour. Zero value is not usable; call
// DefaultOptions and override, or set fields explicitly.
type Options struct {
	// MinChars only deduplicates blocks at least this large (bytes).
	MinChars int
	// ProtectLast never stubs inside the last N messages. 0 = prefix-stable
	// (append-invariant). Combines with MinChars.
	ProtectLast int
	// StubRoles lists roles whose content may be replaced with a stub.
	// Assistant/system content is REGISTERED (so later duplicates elsewhere
	// can reference it) but never modified.
	StubRoles []string
}

// DefaultOptions returns the reference defaults matching the Python original:
// MinChars=800, ProtectLast=2, StubRoles=[tool, function, user].
func DefaultOptions() Options {
	return Options{
		MinChars:    800,
		ProtectLast: 2,
		StubRoles:   []string{"tool", "function", "user"},
	}
}

// Stats reports how many blocks were stubbed and how much was saved.
type Stats struct {
	Blocks         int
	CharsSaved     int
	EstTokensSaved int // chars/4 heuristic, logging only
}

// Message is a single chat-completion message. Modeled as a generic map so the
// port accepts any OpenAI-compatible schema (string content OR the content-parts
// list form) without type coupling.
type Message = map[string]interface{}

// DeduplicateMessages returns (newMessages, stats). Input objects are never
// mutated — a fresh slice and shallow-copied maps are returned. Behaviour and
// output are byte-identical to the Python dedup_messages for the same input
// (modulo the documented byte-vs-char length diff on multibyte content).
func DeduplicateMessages(messages []Message, opts Options) ([]Message, Stats) {
	if opts.MinChars <= 0 {
		opts.MinChars = 800
	}
	if opts.ProtectLast < 0 {
		opts.ProtectLast = 0
	}
	if len(opts.StubRoles) == 0 {
		opts.StubRoles = []string{"tool", "function", "user"}
	}

	n := len(messages)
	protectFrom := n - opts.ProtectLast
	if protectFrom < 0 {
		protectFrom = 0
	}
	if protectFrom > n {
		protectFrom = n
	}

	seen := map[string]int{} // digest -> index of first occurrence
	out := make([]Message, n)
	copy(out, messages)

	var stats Stats

	for i, msg := range messages {
		if msg == nil {
			continue
		}
		content, hasContent := msg["content"]
		canStub := roleCanStub(msg["role"], opts.StubRoles) && i < protectFrom
		if !hasContent {
			continue
		}

		// String content: the common OpenAI form (a tool result / file dump).
		if s, ok := content.(string); ok {
			if stub, ok := maybeStub(s, i, canStub, opts, seen, &stats); ok {
				m2 := copyMessage(msg)
				m2["content"] = stub
				out[i] = m2
			}
			continue
		}

		// Content-parts list: the OpenAI "array of {type, text} parts" form.
		if parts, ok := content.([]interface{}); ok {
			var newParts []interface{}
			for j, p := range parts {
				pm, ok := p.(map[string]interface{})
				if !ok {
					continue
				}
				if pm["type"] != "text" {
					continue
				}
				t, ok := pm["text"].(string)
				if !ok {
					continue
				}
				if stub, ok := maybeStub(t, i, canStub, opts, seen, &stats); ok {
					if newParts == nil {
						newParts = make([]interface{}, len(parts))
						copy(newParts, parts)
					}
					p2 := copyMessage(pm)
					p2["text"] = stub
					newParts[j] = p2
				}
			}
			if newParts != nil {
				m2 := copyMessage(msg)
				m2["content"] = newParts
				out[i] = m2
			}
		}
	}

	stats.EstTokensSaved = stats.CharsSaved / 4
	return out, stats
}

// maybeStub registers first occurrences and produces a stub for later
// duplicates. Mirrors the Python closure exactly: registration happens for any
// role; stubbing only when canStub.
func maybeStub(text string, msgIndex int, canStub bool, opts Options, seen map[string]int, stats *Stats) (string, bool) {
	if len(text) < opts.MinChars {
		return "", false
	}
	h := digest(text)
	first, ok := seen[h]
	if !ok {
		seen[h] = msgIndex // register first occurrence (any role)
		return "", false
	}
	if !canStub {
		return "", false
	}
	stub := fmt.Sprintf(defaultStubTemplate, len(text), h, first)
	if len(stub) >= len(text) {
		return "", false
	}
	stats.Blocks++
	stats.CharsSaved += len(text) - len(stub)
	return stub, true
}

// roleCanStub reports whether role may be stubbed. System is never stubbed.
func roleCanStub(role interface{}, stubRoles []string) bool {
	r, ok := role.(string)
	if !ok || r == "system" {
		return false
	}
	for _, s := range stubRoles {
		if r == s {
			return true
		}
	}
	return false
}

// digest returns the first 16 hex chars of the sha256 of text, matching
// Python's hashlib.sha256(...).hexdigest()[:16].
func digest(text string) string {
	sum := sha256.Sum256([]byte(text))
	return hex.EncodeToString(sum[:])[:16]
}

// copyMessage returns a shallow copy of m (the same copy semantics as
// Python's copy.copy).
func copyMessage(m Message) Message {
	out := make(Message, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}
