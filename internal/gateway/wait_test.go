package gateway

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/auth"
	"github.com/modelcontextprotocol/go-sdk/mcp"

	"sshmcp/internal/runner"
)

type functionRPC func(context.Context, runner.Request) (runner.Response, error)

func (f functionRPC) Call(ctx context.Context, request runner.Request) (runner.Response, error) {
	return f(ctx, request)
}

func authenticatedRequest() *mcp.CallToolRequest {
	return &mcp.CallToolRequest{Extra: &mcp.RequestExtra{TokenInfo: &auth.TokenInfo{
		Extra: map[string]any{"owner": runner.Owner{Issuer: "issuer", Subject: "subject"}},
	}}}
}

func TestWaitedExecuteReturnsBoundedOutputWithoutResubmission(t *testing.T) {
	var requests []runner.Request
	s := server{rpc: functionRPC(func(_ context.Context, request runner.Request) (runner.Response, error) {
		requests = append(requests, request)
		job := &runner.Job{ID: strings.Repeat("b", 32), State: runner.Accepted}
		if request.Action == "run" {
			return runner.Response{Job: job, Duplicate: true}, nil
		}
		job.State = runner.Completed
		return runner.Response{Job: job, Output: &runner.Output{Stdout: []byte("abc"), StdoutCursor: 3, StdoutFinal: true, StderrFinal: true}}, nil
	})}
	result, _, err := s.run(t.Context(), authenticatedRequest(), RunInput{
		Action: "execute", ConnectionID: strings.Repeat("a", 32), IdempotencyKey: "original-key", Command: "printf abcdef", WaitMillis: 1000, Limit: 3,
	})
	if err != nil || result.IsError {
		t.Fatalf("waited execute failed: %v, %+v", err, result)
	}
	data := result.StructuredContent.(map[string]any)
	if len(requests) != 2 || requests[0].Action != "run" || requests[1].Action != "output" ||
		requests[1].Owner != requests[0].Owner || requests[1].JobID != strings.Repeat("b", 32) || requests[1].Limit != 3 {
		t.Fatalf("wait must only read the authenticated acknowledged job: %+v", requests)
	}
	stdout := data["output"].(map[string]any)["stdout"].(map[string]any)
	if data["job"].(map[string]any)["state"] != runner.Completed || data["duplicate"] != true || stdout["text"] != "abc" || stdout["next_cursor"] != int64(3) {
		t.Fatalf("wait lost completion, duplicate or output cursor: %+v", data)
	}
}

func TestWaitedStatusReturnsOnNewOutputOrTerminalState(t *testing.T) {
	for _, state := range []string{runner.Running, runner.Completed} {
		t.Run(state, func(t *testing.T) {
			calls := 0
			s := server{rpc: functionRPC(func(_ context.Context, request runner.Request) (runner.Response, error) {
				calls++
				if request.Action != "output" || request.StdoutCursor != 4 || request.StderrCursor != 7 || request.Limit != 2 || request.Owner.Subject != "subject" {
					t.Fatalf("status poll changed identity, cursor or bound: %+v", request)
				}
				response := runner.Response{Job: &runner.Job{ID: request.JobID, State: runner.Running}, Output: &runner.Output{StdoutCursor: 4, StderrCursor: 7}}
				if calls == 2 {
					response.Job.State = state
					if state == runner.Running {
						response.Output.Stderr = []byte{0, 255}
						response.Output.StderrCursor = 9
					}
				}
				return response, nil
			})}
			result, _, err := s.run(t.Context(), authenticatedRequest(), RunInput{
				Action: "status", JobID: strings.Repeat("b", 32), StdoutCursor: 4, StderrCursor: 7, Limit: 2, WaitMillis: 1000,
			})
			if err != nil || result.IsError || calls != 2 {
				t.Fatalf("status did not return on new output/terminal: calls=%d, result=%+v, err=%v", calls, result, err)
			}
			if state == runner.Running {
				stderr := result.StructuredContent.(map[string]any)["output"].(map[string]any)["stderr"].(map[string]any)
				if stderr["base64"] != "AP8=" || stderr["next_cursor"] != int64(9) {
					t.Fatalf("binary output or cursor changed: %+v", stderr)
				}
			}
		})
	}
}

func TestWaitTimeoutCancellationAndReadFailureKeepAcknowledgedJob(t *testing.T) {
	for _, cause := range []string{"timeout", "caller-cancelled", "read-failed", "read-failed-at-timeout"} {
		t.Run(cause, func(t *testing.T) {
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			runs := 0
			s := server{rpc: functionRPC(func(ctx context.Context, request runner.Request) (runner.Response, error) {
				if request.Action == "run" {
					runs++
					return runner.Response{Job: &runner.Job{ID: strings.Repeat("b", 32), State: runner.Accepted}}, nil
				}
				if request.Action != "output" {
					t.Fatalf("wait changed the job: %+v", request)
				}
				if cause == "read-failed" {
					return runner.Response{}, errors.New("private transport details")
				}
				if cause == "caller-cancelled" {
					cancel()
				}
				<-ctx.Done()
				if cause == "read-failed-at-timeout" {
					return runner.Response{}, errors.New("private transport details")
				}
				return runner.Response{}, ctx.Err()
			})}
			started := time.Now()
			result, _, err := s.run(ctx, authenticatedRequest(), RunInput{
				Action: "execute", ConnectionID: strings.Repeat("a", 32), IdempotencyKey: "once", Command: "sleep 30", WaitMillis: 50,
			})
			if err != nil || result.IsError || runs != 1 || time.Since(started) > time.Second {
				t.Fatalf("wait was unbounded, changed execution, or lost acknowledgement: %+v, %v", result, err)
			}
			data := result.StructuredContent.(map[string]any)
			if data["job"].(map[string]any)["job_id"] != strings.Repeat("b", 32) {
				t.Fatalf("wait lost accepted job ID: %+v", data)
			}
			_, failed := data["wait_error"]
			if failed != (cause != "timeout") {
				t.Fatalf("unexpected wait diagnostic: %+v", data)
			}
		})
	}
}

func TestSixteenWaitedExecutionsReleaseOrdinaryRPCSlots(t *testing.T) {
	var mu sync.Mutex
	accepted, completed := 0, 0
	slots := make(chan struct{}, 6)
	s := server{rpc: functionRPC(func(ctx context.Context, request runner.Request) (runner.Response, error) {
		select {
		case slots <- struct{}{}:
			defer func() { <-slots }()
		case <-ctx.Done():
			return runner.Response{}, ctx.Err()
		}
		time.Sleep(5 * time.Millisecond)
		mu.Lock()
		defer mu.Unlock()
		if request.Action == "run" {
			accepted++
			return runner.Response{Job: &runner.Job{ID: request.ConnectionID, State: runner.Accepted}}, nil
		}
		state := runner.Running
		if accepted == 16 {
			state = runner.Completed
			completed++
		}
		return runner.Response{Job: &runner.Job{ID: request.JobID, State: state}, Output: &runner.Output{}}, nil
	})}
	var jobs sync.WaitGroup
	for i := range 16 {
		jobs.Go(func() {
			result, _, err := s.run(t.Context(), authenticatedRequest(), RunInput{
				Action: "execute", ConnectionID: fmt.Sprintf("%032x", i), IdempotencyKey: fmt.Sprint(i), Command: "sleep 1", WaitMillis: 2000,
			})
			if err != nil || result.IsError || result.StructuredContent.(map[string]any)["job"].(map[string]any)["state"] != runner.Completed {
				t.Errorf("six-slot transport serialized waited execution: %+v, %v", result, err)
			}
		})
	}
	jobs.Wait()
	if accepted != 16 || completed != 16 {
		t.Fatalf("waiters prevented sixteen overlapping jobs: accepted=%d completed=%d", accepted, completed)
	}
}
