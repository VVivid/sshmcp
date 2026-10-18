package runner

import (
	"os"
	"path/filepath"
	"testing"
)

func TestShellCompletionLossAndMalformedRecords(t *testing.T) {
	for _, tc := range []struct {
		name, command, reason string
		persistentErrexit     bool
	}{
		{name: "errexit", command: "set -e; false", reason: "shell_completion_missing"},
		{name: "persistent-errexit", command: "false", reason: "shell_completion_missing", persistentErrexit: true},
		{name: "exit-zero", command: "exit 0", reason: "shell_completion_missing"},
		{name: "exit-nonzero", command: "exit 7", reason: "shell_completion_missing"},
		{name: "closed-descriptor", command: "exec 3>&-; sleep 1", reason: "shell_completion_missing"},
		{name: "malformed-record", command: "printf 'malformed\\n' >&3; sleep 1", reason: "shell_control_corrupted"},
		{name: "oversized-record", command: "printf '%070000d\\n' 0 >&3; sleep 1", reason: "shell_control_corrupted"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			e, config, _ := setup(t, nil)
			connection := connect(t, e)
			if tc.persistentErrexit {
				j := submit(t, e, connection, "enable-errexit", "set -e")
				if got := waitJob(t, e, j.ID); got.State != Completed {
					t.Fatal(got)
				}
			}
			marker := filepath.Join(config.Root, "executions")
			command := "printf once >> " + shellQuote(marker) + "; " + tc.command
			accepted := submit(t, e, connection, "lost", command)
			job := waitJob(t, e, accepted.ID)
			if job.State != Lost || job.Reason != tc.reason || !job.ShellStateLost || job.ExitCode != nil {
				t.Fatalf("untrustworthy completion must not become success or an invented exit code: %+v", job)
			}
			duplicate := request(t, e, Request{Action: "run", ConnectionID: connection, IdempotencyKey: "lost", Command: command, Limit: 3})
			if !duplicate.Duplicate || duplicate.Job.ID != accepted.ID || duplicate.Job.State != Lost {
				t.Fatalf("lost job was not retained: %+v", duplicate)
			}
			if data, err := os.ReadFile(marker); err != nil || string(data) != "once" {
				t.Fatalf("lost job replayed or did not reach its side effect: %q, %v", data, err)
			}
			result := e.Handle(t.Context(), Request{Action: "run", Owner: testOwner, ConnectionID: connection, IdempotencyKey: "next", Command: "true"})
			if result.Code != "shell_lost" {
				t.Fatalf("lost shell accepted new work: %+v", result)
			}
		})
	}
}
