package runner

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestNativeRPCRejectsMCPOnlyWaitOption(t *testing.T) {
	for _, value := range []string{"-1", "0", "10000", "10001"} {
		var request Request
		if err := decode(json.RawMessage(`{"action":"output","wait_ms":`+value+`}`), &request); err == nil {
			t.Fatalf("native RPC silently accepted MCP-only wait_ms=%s", value)
		}
	}
}

func TestLargeCommandRPCBoundsAndDurability(t *testing.T) {
	const escapedTail = "\nprintf escaped"
	for _, tc := range []struct {
		name, command, output string
		sourceBytes           int
	}{
		{"tabs-and-output", ":" + strings.Repeat("\t", 1043498) + "\nprintf \"%8192s\" x", strings.Repeat(" ", 8191) + "x", 1043517},
		{"html-escaping", "#" + strings.Repeat(">", 1048014-1-len(escapedTail)) + escapedTail, "escaped", 1048014},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if len(tc.command) != tc.sourceBytes {
				t.Fatal("regression source has the wrong byte length")
			}
			e, config, supervision := setup(t, nil)
			ctx, cancel := context.WithCancel(t.Context())
			done := make(chan error, 1)
			go func() { done <- e.Serve(ctx) }()
			stopRPC := sync.OnceFunc(func() {
				cancel()
				if err := <-done; err != nil {
					t.Error(err)
				}
			})
			t.Cleanup(stopRPC)
			deadline := time.Now().Add(2 * time.Second)
			for {
				if _, err := os.Stat(config.Socket); err == nil {
					break
				}
				if time.Now().After(deadline) {
					t.Fatal("RPC socket did not become ready")
				}
				time.Sleep(10 * time.Millisecond)
			}
			call := func(r Request) Response {
				t.Helper()
				r.Owner = testOwner
				var result Response
				if err := Call(t.Context(), config.Socket, r, &result); err != nil {
					t.Fatalf("%s RPC: %v", r.Action, err)
				}
				if result.Error != "" {
					t.Fatalf("%s: %s (%s)", r.Action, result.Error, result.Code)
				}
				return result
			}
			connection := call(Request{Action: "connect"}).Connection.ID
			run := Request{Action: "run", Owner: testOwner, ConnectionID: connection, IdempotencyKey: tc.name, Command: tc.command}
			payload, err := json.Marshal(run)
			if err != nil || len(payload)+1 > MaxRequestBytes {
				t.Fatalf("request outside declared budget: bytes=%d, err=%v", len(payload)+1, err)
			}
			if tc.name == "html-escaping" && len(payload) <= 2<<20 {
				t.Fatal("regression no longer crosses the original request cap")
			}
			job := call(run).Job
			if job.Command != "" {
				t.Fatal("wire acknowledgement contains command source")
			}
			finished := waitJob(t, e, job.ID)
			if finished.State != Completed || finished.Command != tc.command {
				t.Fatalf("internal execution changed source or failed: state=%s, source_bytes=%d", finished.State, len(finished.Command))
			}
			status := call(Request{Action: "output", JobID: job.ID, Limit: 8192})
			if string(status.Output.Stdout) != tc.output || len(status.Output.Stderr) != 0 {
				t.Fatalf("unexpected output: stdout_bytes=%d, stderr_bytes=%d", len(status.Output.Stdout), len(status.Output.Stderr))
			}
			encoded, err := json.Marshal(status)
			if err != nil || len(encoded) > MaxResponseBytes || bytes.Contains(encoded, []byte(`"command":`)) {
				t.Fatalf("unbounded response or source leak: bytes=%d, err=%v", len(encoded), err)
			}
			duplicate := call(run)
			if !duplicate.Duplicate || duplicate.Job.ID != job.ID {
				t.Fatal("wire duplicate did not retain the original job")
			}
			stopRPC()
			if err := e.Close(); err != nil {
				t.Fatal(err)
			}
			reopened, err := openEngine(config, supervision)
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() {
				if err := reopened.Close(); err != nil {
					t.Error(err)
				}
			})
			retained := request(t, reopened, Request{Action: "lookup", IdempotencyKey: run.IdempotencyKey})
			if retained.Job.Command != tc.command {
				t.Fatal("restart lost durable command source")
			}
			stored := reopened.db.Jobs[job.ID]
			if _, err := json.Marshal(Response{Job: stored}); err != nil || stored.Command != tc.command {
				t.Fatal("response projection changed the stored job")
			}
			duplicate = request(t, reopened, run)
			if !duplicate.Duplicate || duplicate.Job.ID != job.ID {
				t.Fatal("restart broke full-source idempotency")
			}
			run.Command += " "
			if got := reopened.Handle(t.Context(), run); got.Code != "idempotency_conflict" {
				t.Fatal("changed source did not conflict after restart")
			}
			data, err := os.ReadFile(filepath.Join(config.Root, "state.json"))
			var db database
			if err != nil || json.Unmarshal(data, &db) != nil || db.Jobs[job.ID].Command != tc.command {
				t.Fatal("durable JSON lost full source")
			}
		})
	}
}
