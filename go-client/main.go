package main

import (
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
)

// ─── Protocol ────────────────────────────────────────────

func socketPath() string {
	// Honor env override
	if p := os.Getenv("TOOLRECALL_TRANSPORT"); p != "" {
		return p
	}
	// XDG_RUNTIME_DIR
	if xdg := os.Getenv("XDG_RUNTIME_DIR"); xdg != "" {
		return filepath.Join(xdg, "toolrecall.sock")
	}
	// Fallback to home dir
	home, err := os.UserHomeDir()
	if err == nil {
		return filepath.Join(home, ".toolrecall", "toolrecall.sock")
	}
	return "/run/user/1000/toolrecall.sock"
}

func dial() (net.Conn, error) {
	return net.Dial("unix", socketPath())
}

func sendMessage(conn net.Conn, data map[string]interface{}) error {
	payload, err := json.Marshal(data)
	if err != nil {
		return fmt.Errorf("marshal: %w", err)
	}
	header := make([]byte, 4)
	binary.BigEndian.PutUint32(header, uint32(len(payload)))
	if _, err := conn.Write(header); err != nil {
		return fmt.Errorf("write header: %w", err)
	}
	if _, err := conn.Write(payload); err != nil {
		return fmt.Errorf("write payload: %w", err)
	}
	return nil
}

func receiveMessage(conn net.Conn) (map[string]interface{}, error) {
	header := make([]byte, 4)
	if _, err := io.ReadFull(conn, header); err != nil {
		return nil, fmt.Errorf("read header: %w", err)
	}
	length := binary.BigEndian.Uint32(header)
	if length > 1024*1024 {
		return nil, fmt.Errorf("message too large: %d bytes", length)
	}
	payload := make([]byte, length)
	if _, err := io.ReadFull(conn, payload); err != nil {
		return nil, fmt.Errorf("read payload: %w", err)
	}
	var result map[string]interface{}
	if err := json.Unmarshal(payload, &result); err != nil {
		return nil, fmt.Errorf("unmarshal: %w", err)
	}
	return result, nil
}

func sendRequest(cmd string, args map[string]interface{}) (map[string]interface{}, error) {
	conn, err := dial()
	if err != nil {
		return nil, fmt.Errorf("connect to daemon: %w\n  Make sure ToolRecall daemon is running: toolrecall daemon &", err)
	}
	defer conn.Close()

	payload := map[string]interface{}{"cmd": cmd}
	for k, v := range args {
		payload[k] = v
	}
	if err := sendMessage(conn, payload); err != nil {
		return nil, fmt.Errorf("send: %w", err)
	}
	return receiveMessage(conn)
}

func printResult(resp map[string]interface{}) {
	if err, ok := resp["error"]; ok {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	for _, key := range []string{"content", "output", "result", "message"} {
		if v, ok := resp[key]; ok {
			switch val := v.(type) {
			case string:
				fmt.Print(val)
				if !strings.HasSuffix(val, "\n") {
					fmt.Println()
				}
			default:
				b, _ := json.MarshalIndent(val, "", "  ")
				fmt.Println(string(b))
			}
			return
		}
	}
	b, _ := json.MarshalIndent(resp, "", "  ")
	fmt.Println(string(b))
}

// ─── Commands ────────────────────────────────────────────

func cmdRead(path string, bypassCache bool) {
	args := map[string]interface{}{"path": path}
	if bypassCache {
		// Send to cache_refresh_file instead of cached_read for fresh reads
		resp, err := sendRequest("cache_refresh_file", args)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		printResult(resp)
		return
	}
	resp, err := sendRequest("cached_read", args)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	printResult(resp)
}

func cmdTerminal(command string, ttl int) {
	args := map[string]interface{}{"command": command}
	if ttl >= 0 {
		args["ttl"] = ttl
	}
	resp, err := sendRequest("cached_terminal", args)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	printResult(resp)
}

func cmdStatus() {
	resp, err := sendRequest("cache_status", nil)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	printResult(resp)
}

func cmdPing() {
	conn, err := dial()
	if err != nil {
		fmt.Println("Daemon: NOT RUNNING")
		os.Exit(1)
	}
	defer conn.Close()
	if err := sendMessage(conn, map[string]interface{}{"cmd": "ping"}); err != nil {
		fmt.Println("Daemon: NOT RUNNING")
		os.Exit(1)
	}
	resp, err := receiveMessage(conn)
	if err != nil || resp == nil {
		fmt.Println("Daemon: NOT RUNNING")
		os.Exit(1)
	}
	if pong, ok := resp["pong"]; ok && pong == true {
		fmt.Println("Daemon: RUNNING")
	} else {
		fmt.Println("Daemon: RESPONDING (unexpected response)")
	}
}

func cmdWrite(path, content string) {
	resp, err := sendRequest("cached_write", map[string]interface{}{
		"path":    path,
		"content": content,
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	printResult(resp)
}

func cmdHelp() {
	fmt.Println(`ToolRecall CLI (Go client) - cached tool operations via UDS

USAGE:
  tr <command> [args]

COMMANDS:
  tr read <path>              Read a file through ToolRecall's cache
  tr read --bypass <path>     Force a fresh read (skip cache)
  tr cat <path>               Alias for read
  tr term <command>           Run a terminal command (cached)
  tr exec <command>           Alias for term
  tr write <path> <content>   Write content to a file (invalidates cache)
  tr status                   Show cache statistics
  tr ping                     Check if daemon is running
  tr help                     Show this help

ENVIRONMENT:
  TOOLRECALL_TRANSPORT  Override UDS socket path

EXAMPLES:
  tr read main.py
  tr cat /etc/os-release
  tr term "hostname"
  tr exec "whoami"
  tr ping`)
}

func main() {
	if len(os.Args) < 2 {
		cmdHelp()
		os.Exit(1)
	}

	switch os.Args[1] {
	case "read", "cat":
		if len(os.Args) < 3 {
			fmt.Fprintln(os.Stderr, "Usage: tr read [--bypass] <path>")
			os.Exit(1)
		}
		bypass := false
		pathIdx := 2
		if os.Args[2] == "--bypass" || os.Args[2] == "--refresh" {
			bypass = true
			pathIdx = 3
		}
		if pathIdx >= len(os.Args) {
			fmt.Fprintln(os.Stderr, "Missing path argument")
			os.Exit(1)
		}
		cmdRead(os.Args[pathIdx], bypass)

	case "term", "exec":
		if len(os.Args) < 3 {
			fmt.Fprintln(os.Stderr, "Usage: tr term <command>")
			os.Exit(1)
		}
		command := strings.Join(os.Args[2:], " ")
		cmdTerminal(command, -1)

	case "write":
		if len(os.Args) < 4 {
			fmt.Fprintln(os.Stderr, "Usage: tr write <path> <content>")
			os.Exit(1)
		}
		cmdWrite(os.Args[2], strings.Join(os.Args[3:], " "))

	case "status":
		cmdStatus()

	case "ping":
		cmdPing()

	case "help", "--help", "-h":
		cmdHelp()

	default:
		fmt.Fprintf(os.Stderr, "Unknown command: %s\n", os.Args[1])
		fmt.Fprintln(os.Stderr, "Run 'tr help' for usage")
		os.Exit(1)
	}
}