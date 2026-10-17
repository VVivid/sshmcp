package runner

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

// Tests use the real worker and native Bash. Only the systemd process launcher
// is replaced on macOS; the live target check exercises real user units.
func TestWorkerProcess(t *testing.T) {
	if os.Getenv("SSHMCP_WORKER_TEST") != "1" {
		return
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	limit, _ := strconv.ParseInt(os.Getenv("SSHMCP_OUTPUT_LIMIT"), 10, 64)
	err := RunWorker(ctx, os.Getenv("SSHMCP_WORKER_ROOT"), os.Getenv("SSHMCP_WORKER_SOCKET"), os.Getenv("SSHMCP_WORKER_HOME"), 80*time.Millisecond, limit)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	os.Exit(0)
}

type testProcess struct {
	cmd  *exec.Cmd
	done chan struct{}
}
type processSupervisor struct {
	mu        sync.Mutex
	config    Config
	processes map[string]*testProcess
}

func (s *processSupervisor) Start(ctx context.Context, id string) error {
	cmd := exec.Command(os.Args[0], "-test.run=^TestWorkerProcess$")
	cmd.Env = append(os.Environ(), "SSHMCP_WORKER_TEST=1", "SSHMCP_WORKER_ROOT="+filepath.Join(s.config.Root, "connections", id), "SSHMCP_WORKER_SOCKET="+workerSocket(s.config, id), "SSHMCP_WORKER_HOME="+s.config.Home, "SSHMCP_OUTPUT_LIMIT="+strconv.FormatInt(s.config.OutputLimit, 10))
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		return err
	}
	process := &testProcess{cmd: cmd, done: make(chan struct{})}
	s.mu.Lock()
	s.processes[id] = process
	s.mu.Unlock()
	go func() { cmd.Wait(); close(process.done) }()
	return nil
}
func (s *processSupervisor) Stop(ctx context.Context, id string) error {
	s.mu.Lock()
	process := s.processes[id]
	s.mu.Unlock()
	if process == nil {
		return nil
	}
	process.cmd.Process.Signal(syscall.SIGTERM)
	select {
	case <-process.done:
		return nil
	case <-ctx.Done():
		process.cmd.Process.Kill()
		return ctx.Err()
	}
}

var testOwner = Owner{Issuer: "opaque issuer", Subject: "opaque subject"}

func setup(t *testing.T, change func(*Config)) (*Engine, Config, *processSupervisor) {
	t.Helper()
	// Unix socket paths have a small platform limit; Go's per-test paths can be
	// longer than the limit before appending a connection ID.
	root, err := os.MkdirTemp("/tmp", "sshmcp-")
	if err != nil {
		t.Fatal(err)
	}
	config := Config{Root: root, Socket: filepath.Join(root, "runner.sock"), Home: root, CancelGrace: 80 * time.Millisecond, CommandTimeout: 5 * time.Second, OutputLimit: 1 << 20}.withDefaults()
	if change != nil {
		change(&config)
	}
	supervision := &processSupervisor{config: config, processes: map[string]*testProcess{}}
	e, err := openEngine(config, supervision)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if !e.closing {
			if err := e.Close(); err != nil {
				t.Error(err)
			}
		}
		os.RemoveAll(root)
	})
	return e, config, supervision
}
func request(t *testing.T, e *Engine, r Request) Response {
	t.Helper()
	if r.Owner == (Owner{}) {
		r.Owner = testOwner
	}
	result := e.Handle(t.Context(), r)
	if result.Error != "" {
		t.Fatalf("%s: %s (%s)", r.Action, result.Error, result.Code)
	}
	return result
}
func connect(t *testing.T, e *Engine) string {
	t.Helper()
	return request(t, e, Request{Action: "connect"}).Connection.ID
}
func submit(t *testing.T, e *Engine, conn, key, command string) *Job {
	t.Helper()
	return request(t, e, Request{Action: "run", ConnectionID: conn, IdempotencyKey: key, Command: command}).Job
}
func waitJob(t *testing.T, e *Engine, id string) *Job {
	t.Helper()
	deadline := time.Now().Add(8 * time.Second)
	for time.Now().Before(deadline) {
		j := request(t, e, Request{Action: "job", JobID: id}).Job
		if terminal(j.State) {
			return j
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("job did not terminate")
	return nil
}
func output(t *testing.T, e *Engine, id string) *Output {
	t.Helper()
	return request(t, e, Request{Action: "output", JobID: id}).Output
}

func TestPersistentStateIsolationAndCursors(t *testing.T) {
	e, config, _ := setup(t, nil)
	one, two := connect(t, e), connect(t, e)
	j := submit(t, e, one, "state", "cd /tmp; retained='hello'; greet() { printf '%s' \"$retained\"; }; printf 'out\\000x'; printf 'error' >&2")
	if got := waitJob(t, e, j.ID); got.State != Completed {
		t.Fatal(got)
	}
	chunk := request(t, e, Request{Action: "output", JobID: j.ID, Limit: 3}).Output
	if string(chunk.Stdout) != "out" || string(chunk.Stderr) != "err" || chunk.StdoutEOF {
		t.Fatalf("first chunk: %+v", chunk)
	}
	next := request(t, e, Request{Action: "output", JobID: j.ID, StdoutCursor: chunk.StdoutCursor, StderrCursor: chunk.StderrCursor, Limit: 3}).Output
	if string(next.Stdout) != "\x00x" || string(next.Stderr) != "or" || !next.StdoutFinal || !next.StderrFinal {
		t.Fatalf("next chunk: %+v", next)
	}
	j = submit(t, e, one, "read-state", "printf '%s|' \"$PWD\"; greet")
	waitJob(t, e, j.ID)
	if got := string(output(t, e, j.ID).Stdout); got != "/tmp|hello" {
		t.Fatal(got)
	}
	j = submit(t, e, two, "isolated", "printf '%s|%s' \"$PWD\" \"${retained-unset}\"; type greet >/dev/null 2>&1")
	if got := waitJob(t, e, j.ID); got.State != Failed || got.ShellStateLost {
		t.Fatal(got)
	}
	physicalHome, err := filepath.EvalSymlinks(config.Home)
	if err != nil {
		t.Fatal(err)
	}
	if got := string(output(t, e, j.ID).Stdout); got != physicalHome+"|unset" {
		t.Fatal(got)
	}
	wrong := e.Handle(t.Context(), Request{Action: "job", Owner: Owner{Issuer: testOwner.Issuer, Subject: "someone else"}, JobID: j.ID})
	if wrong.Code != "not_found" {
		t.Fatal(wrong)
	}
	missing := e.Handle(t.Context(), Request{Action: "connect"})
	if missing.Error == "" {
		t.Fatal("missing owner accepted")
	}
	j = submit(t, e, one, "errexit", "set -e; false")
	if got := waitJob(t, e, j.ID); got.State != Lost || !got.ShellStateLost {
		t.Fatal(got)
	}
	lost := e.Handle(t.Context(), Request{Action: "run", Owner: testOwner, ConnectionID: one, IdempotencyKey: "after-exit", Command: "true"})
	if lost.Code != "shell_lost" {
		t.Fatal(lost)
	}
}

func TestDurableDedupConcurrencyAndCapacity(t *testing.T) {
	e, _, _ := setup(t, nil)
	connections := make([]string, 17)
	for i := range connections {
		connections[i] = connect(t, e)
	}
	jobs := make([]*Job, 16)
	for i := range jobs {
		jobs[i] = submit(t, e, connections[i], fmt.Sprintf("parallel-%d", i), "sleep 1")
	}
	duplicate := request(t, e, Request{Action: "run", ConnectionID: connections[0], IdempotencyKey: "parallel-0", Command: "sleep 1"})
	if !duplicate.Duplicate || duplicate.Job.ID != jobs[0].ID {
		t.Fatal(duplicate)
	}
	for _, tc := range []struct{ conn, key, command, code string }{
		{connections[0], "other-key", "true", "connection_busy"},
		{connections[0], "parallel-0", "false", "idempotency_conflict"},
		{connections[16], "overflow", "true", "capacity"},
	} {
		got := e.Handle(t.Context(), Request{Action: "run", Owner: testOwner, ConnectionID: tc.conn, IdempotencyKey: tc.key, Command: tc.command})
		if got.Code != tc.code {
			t.Fatalf("want %s: %+v", tc.code, got)
		}
	}
	for _, j := range jobs {
		if got := waitJob(t, e, j.ID); got.State != Completed {
			t.Fatal(got)
		}
	}
	if got := request(t, e, Request{Action: "lookup", IdempotencyKey: "parallel-0"}).Job.ID; got != jobs[0].ID {
		t.Fatal(got)
	}
}

func TestIndependentCancellationDeadlinesAndOutputQuota(t *testing.T) {
	e, _, _ := setup(t, func(c *Config) { c.OutputLimit = 1024 })
	first, second := connect(t, e), connect(t, e)
	j := submit(t, e, first, "cancel", "trap '' INT; printf ready; while :; do sleep 10; done")
	deadline := time.Now().Add(3 * time.Second)
	for len(output(t, e, j.ID).Stdout) == 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	start := time.Now()
	request(t, e, Request{Action: "cancel", JobID: j.ID})
	if time.Since(start) > time.Second {
		t.Fatal("cancel RPC blocked on command")
	}
	other := submit(t, e, second, "during-cancel", "printf independent")
	if got := waitJob(t, e, other.ID); got.State != Completed {
		t.Fatal(got)
	}
	if got := waitJob(t, e, j.ID); got.State != Cancelled || !got.ShellStateLost {
		t.Fatal(got)
	}
	third := connect(t, e)
	timed := request(t, e, Request{Action: "run", ConnectionID: third, IdempotencyKey: "deadline", Command: "sleep 10", TimeoutMillis: 50}).Job
	if got := waitJob(t, e, timed.ID); got.State != Failed || got.Reason != "command_timeout" || !got.ShellStateLost {
		t.Fatal(got)
	}
	fourth := connect(t, e)
	quota := submit(t, e, fourth, "quota", "yes output")
	if got := waitJob(t, e, quota.ID); got.State != Failed || got.Reason != "output_limit" {
		t.Fatal(got)
	}
	if got := len(output(t, e, quota.ID).Stdout); got != 1024 {
		t.Fatal(got)
	}
}

func TestBackgroundOutputAndRestartRecovery(t *testing.T) {
	e, config, supervision := setup(t, nil)
	c := connect(t, e)
	j := submit(t, e, c, "background", "(sleep 1; printf late) & printf early")
	waitJob(t, e, j.ID)
	if got := output(t, e, j.ID); string(got.Stdout) != "early" || got.StdoutFinal {
		t.Fatalf("background output premature final: %+v", got)
	}
	next := submit(t, e, c, "next", "printf next")
	waitJob(t, e, next.ID)
	if got := string(output(t, e, next.ID).Stdout); got != "next" {
		t.Fatal(got)
	}
	time.Sleep(time.Second)
	if got := output(t, e, j.ID); string(got.Stdout) != "earlylate" || !got.StdoutFinal {
		t.Fatalf("background output: %+v", got)
	}
	if err := e.Close(); err != nil {
		t.Fatal(err)
	}
	// Model a crash after acknowledgement, before the command was dispatched.
	// Recovery must retain the key but never execute its persisted command.
	var db database
	data, err := os.ReadFile(filepath.Join(config.Root, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &db); err != nil {
		t.Fatal(err)
	}
	pending := &Job{ID: newID(), ConnectionID: c, Owner: testOwner, IdempotencyKey: "uncertain-ack", Command: "touch " + shellQuote(filepath.Join(config.Root, "must-not-exist")), TimeoutMillis: 5000, State: Accepted, AcceptedAt: time.Now()}
	db.Jobs[pending.ID] = pending
	db.Connections[c].State = Active
	db.Connections[c].BusyJob = pending.ID
	data, _ = json.Marshal(db)
	if err := atomicFile(filepath.Join(config.Root, "state.json"), data); err != nil {
		t.Fatal(err)
	}
	recovered, err := openEngine(config, supervision)
	if err != nil {
		t.Fatal(err)
	}
	defer recovered.Close()
	got := request(t, recovered, Request{Action: "lookup", IdempotencyKey: "uncertain-ack"}).Job
	if got.State != Lost || got.Reason != "runner_restarted" {
		t.Fatal(got)
	}
	retry := request(t, recovered, Request{Action: "run", ConnectionID: c, IdempotencyKey: pending.IdempotencyKey, Command: pending.Command})
	if !retry.Duplicate || retry.Job.ID != pending.ID {
		t.Fatal(retry)
	}
	if _, err := os.Stat(filepath.Join(config.Root, "must-not-exist")); !os.IsNotExist(err) {
		t.Fatal("replayed command")
	}
	if got := string(output(t, recovered, j.ID).Stdout); got != "earlylate" {
		t.Fatal(got)
	}
}

func TestIdleLifetimeRetentionAndClientDisconnect(t *testing.T) {
	e, config, _ := setup(t, func(c *Config) {
		c.IdleTimeout = 100 * time.Millisecond
		c.MaxLifetime = 500 * time.Millisecond
		c.ResultTTL = time.Second
	})
	idle := connect(t, e)
	e.Reap(time.Now().Add(200 * time.Millisecond))
	if got := request(t, e, Request{Action: "connection", ConnectionID: idle}).Connection; got.State != Lost || got.Reason != "idle_timeout" {
		t.Fatal(got)
	}
	active := connect(t, e)
	j := submit(t, e, active, "lifetime", "sleep 10")
	e.Reap(time.Now().Add(time.Second))
	if got := waitJob(t, e, j.ID); got.State != Failed || got.Reason != "max_lifetime" {
		t.Fatal(got)
	}
	e.Reap(time.Now().Add(3 * time.Second))
	if got := e.Handle(t.Context(), Request{Action: "lookup", Owner: testOwner, IdempotencyKey: "lifetime"}); got.Code != "not_found" {
		t.Fatal(got)
	}
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- e.Serve(ctx) }()
	for i := range 100 {
		if _, err := os.Stat(config.Socket); err == nil {
			break
		}
		if i == 99 {
			t.Fatal("socket not ready")
		}
		time.Sleep(10 * time.Millisecond)
	}
	var connected Response
	if err := Call(t.Context(), config.Socket, Request{Action: "connect", Owner: testOwner}, &connected); err != nil {
		t.Fatal(err)
	}
	// A client may drop its acknowledgement. Retrying/lookup uses durable state.
	var accepted Response
	if err := Call(t.Context(), config.Socket, Request{Action: "run", Owner: testOwner, ConnectionID: connected.Connection.ID, IdempotencyKey: "ssh-loss", Command: "sleep .1; printf survives"}, &accepted); err != nil {
		t.Fatal(err)
	}
	if got := waitJob(t, e, accepted.Job.ID); got.State != Completed {
		t.Fatal(got)
	}
	if got := string(output(t, e, accepted.Job.ID).Stdout); !strings.Contains(got, "survives") {
		t.Fatal(got)
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestDurableStoreRefusesConcurrentAndMalformedState(t *testing.T) {
	e, config, supervision := setup(t, nil)
	if second, err := openEngine(config, supervision); err == nil {
		second.Close()
		t.Fatal("second daemon acquired the same durable store")
	}
	connection := connect(t, e)
	job := submit(t, e, connection, "protected-record", "printf stored")
	waitJob(t, e, job.ID)
	if err := e.Close(); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(config.Root, "state.json")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var db database
	if err := json.Unmarshal(data, &db); err != nil {
		t.Fatal(err)
	}
	db.Jobs[job.ID].Owner.Subject = "different persisted owner"
	data, err = json.Marshal(db)
	if err != nil {
		t.Fatal(err)
	}
	if err := atomicFile(path, data); err != nil {
		t.Fatal(err)
	}
	if corrupted, err := openEngine(config, supervision); err == nil {
		corrupted.Close()
		t.Fatal("inconsistent persisted ownership was accepted")
	}
	unchanged, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(unchanged) != string(data) {
		t.Fatal("refused store was rewritten")
	}
}

func TestReadinessRPCDoesNotAllocateAndRejectsUnusableEngine(t *testing.T) {
	e, config, _ := setup(t, func(c *Config) { c.ReapInterval = time.Hour })
	health := Request{Action: "health", Owner: testOwner}
	var result Response
	if err := Call(t.Context(), config.Socket, health, &result); err == nil {
		t.Fatal("runner without a serving RPC socket reported ready")
	}
	before, err := os.ReadFile(filepath.Join(config.Root, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() { done <- e.Serve(ctx) }()
	t.Cleanup(func() {
		cancel()
		if err := <-done; err != nil {
			t.Error(err)
		}
	})
	readyCtx, readyCancel := context.WithTimeout(t.Context(), 2*time.Second)
	defer readyCancel()
	for {
		if err := Call(readyCtx, config.Socket, health, &result); err == nil {
			break
		}
		if readyCtx.Err() != nil {
			t.Fatal("RPC socket did not become ready")
		}
		time.Sleep(10 * time.Millisecond)
	}
	if result.Error != "" {
		t.Fatal(result)
	}
	var missingOwner Response
	if err := Call(t.Context(), config.Socket, Request{Action: "health"}, &missingOwner); err != nil {
		t.Fatal(err)
	}
	if missingOwner.Error == "" {
		t.Fatal("readiness accepted a missing owner")
	}
	e.mu.Lock()
	e.fatal = errors.New("durable storage failed")
	e.mu.Unlock()
	var fatal Response
	if err := Call(t.Context(), config.Socket, health, &fatal); err != nil {
		t.Fatal(err)
	}
	if fatal.Error != "durable storage failed" {
		t.Fatalf("fatal engine reported ready: %+v", fatal)
	}
	after, err := os.ReadFile(filepath.Join(config.Root, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	if string(before) != string(after) || len(e.db.Connections) != 0 || len(e.db.Jobs) != 0 {
		t.Fatal("readiness changed durable state or allocated execution records")
	}
}
