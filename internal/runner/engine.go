package runner

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

type Engine struct {
	mu         sync.Mutex
	config     Config
	store      *store
	db         database
	supervisor supervisor
	fatal      error
	closing    bool
	jobs       sync.WaitGroup
}

func Open(config Config) (*Engine, error) {
	config = config.withDefaults()
	return openEngine(config, systemdSupervisor{config: config})
}

func openEngine(config Config, supervision supervisor) (*Engine, error) {
	config = config.withDefaults()
	if config.IdleTimeout <= 0 || config.CommandTimeout < time.Millisecond || config.MaxLifetime <= 0 || config.ResultTTL <= 0 || config.CancelGrace <= 0 || config.ReapInterval <= 0 || config.OutputLimit <= 0 || config.MaxRecords < 16 {
		return nil, errors.New("timeouts and output limit must be positive; max records must be at least 16")
	}
	s, db, err := openStore(config.Root)
	if err != nil {
		return nil, err
	}
	e := &Engine{config: config, store: s, db: db, supervisor: supervision}
	// The persisted acknowledgement is authoritative. Recovery never resubmits
	// commands, including accepted jobs whose dispatch may have been uncertain.
	var recovery sync.WaitGroup
	var recoveryMu sync.Mutex
	var recoveryErrors []error
	for _, c := range db.Connections {
		if c.State != Active && !c.StopPending {
			continue
		}
		recovery.Go(func() {
			if err := stopShell(supervision, c.ID); err != nil {
				recoveryMu.Lock()
				recoveryErrors = append(recoveryErrors, err)
				recoveryMu.Unlock()
			}
		})
	}
	recovery.Wait()
	if err := errors.Join(recoveryErrors...); err != nil {
		s.close()
		return nil, err
	}
	for _, c := range db.Connections {
		if c.State == Active {
			c.State, c.Reason, c.BusyJob = Lost, "runner_restarted", ""
		}
		c.StopPending = false
	}
	for _, j := range db.Jobs {
		if !terminal(j.State) {
			j.State, j.Reason, j.ShellStateLost, j.FinishedAt = Lost, "runner_restarted", true, time.Now().UTC()
		}
	}
	if err := s.save(db); err != nil {
		s.close()
		return nil, err
	}
	return e, nil
}

func (e *Engine) persist() error {
	if err := e.store.save(e.db); err != nil {
		e.fatal = fmt.Errorf("durable state write failed; restart after repairing storage: %w", err)
		return e.fatal
	}
	return nil
}

func (e *Engine) check() error {
	if e.fatal != nil {
		return e.fatal
	}
	if e.closing {
		return errors.New("runner is shutting down")
	}
	return nil
}

func (e *Engine) Handle(ctx context.Context, request Request) Response {
	if !request.Owner.valid() {
		return responseError(errors.New("nonempty opaque owner issuer and subject are required"))
	}
	switch request.Action {
	case "health":
		// Readiness shares the owner requirement but never creates or touches
		// connection/job records, including their idle and retention timers.
		e.mu.Lock()
		defer e.mu.Unlock()
		if err := e.check(); err != nil {
			return responseError(err)
		}
		return Response{}
	case "connect":
		c, err := e.connect(ctx, request.Owner)
		if err != nil {
			return responseError(err)
		}
		return Response{Connection: c}
	case "run":
		j, duplicate, err := e.submit(request)
		if err != nil {
			return responseError(err)
		}
		return Response{Job: j, Duplicate: duplicate}
	case "job", "lookup", "output":
		return e.lookup(request)
	case "connection":
		e.mu.Lock()
		defer e.mu.Unlock()
		if err := e.check(); err != nil {
			return responseError(err)
		}
		c, err := e.connection(request.Owner, request.ConnectionID)
		if err != nil {
			return responseError(err)
		}
		return Response{Connection: copyConnection(c)}
	case "cancel":
		return e.cancel(ctx, request, "cancelled")
	case "disconnect":
		return e.disconnect(request)
	default:
		return responseError(errors.New("unknown action"))
	}
}

func responseError(err error) Response {
	code := "invalid_request"
	switch {
	case errors.Is(err, ErrNotFound):
		code = "not_found"
	case errors.Is(err, ErrConflict):
		code = "idempotency_conflict"
	case errors.Is(err, ErrBusy):
		code = "connection_busy"
	case errors.Is(err, ErrCapacity):
		code = "capacity"
	case errors.Is(err, ErrLost):
		code = "shell_lost"
	}
	return Response{Error: err.Error(), Code: code}
}

func copyConnection(c *Connection) *Connection { copy := *c; return &copy }
func copyJob(j *Job) *Job                      { copy := *j; return &copy }

func (e *Engine) connection(owner Owner, id string) (*Connection, error) {
	c := e.db.Connections[id]
	if c == nil || c.Owner != owner {
		return nil, ErrNotFound
	}
	return c, nil
}

func (e *Engine) connect(ctx context.Context, owner Owner) (*Connection, error) {
	e.mu.Lock()
	if err := e.check(); err != nil {
		e.mu.Unlock()
		return nil, err
	}
	if len(e.db.Connections) >= e.config.MaxRecords {
		e.mu.Unlock()
		return nil, errors.New("retained connection quota reached; retry after retention expiry")
	}
	now := time.Now().UTC()
	c := &Connection{ID: newID(), Owner: owner, State: Active, CreatedAt: now, LastActivity: now, StopPending: true}
	e.db.Connections[c.ID] = c
	// Reserve the connection durably before creating a supervised process. A
	// failed or uncertain start is a lost connection, never an adopted shell.
	if err := e.persist(); err != nil {
		e.mu.Unlock()
		return nil, err
	}
	e.mu.Unlock()
	startCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	err := e.supervisor.Start(startCtx, c.ID)
	if err == nil {
		for {
			var result workerResult
			err = Call(startCtx, workerSocket(e.config, c.ID), workerRequest{Action: "ready"}, &result)
			if err == nil && result.Error == "" {
				break
			}
			select {
			case <-startCtx.Done():
				err = startCtx.Err()
				goto started
			case <-time.After(20 * time.Millisecond):
			}
		}
	}
started:
	e.mu.Lock()
	if err != nil {
		c.State, c.Reason = Lost, "shell_start_failed"
		e.mu.Unlock()
		stopErr := stopShell(e.supervisor, c.ID)
		e.mu.Lock()
		c.StopPending = stopErr != nil
		if saveErr := e.persist(); saveErr != nil {
			e.mu.Unlock()
			return nil, saveErr
		}
		e.mu.Unlock()
		return nil, fmt.Errorf("start persistent shell: %w", err)
	}
	if e.closing {
		e.mu.Unlock()
		stopShell(e.supervisor, c.ID)
		return nil, errors.New("runner is shutting down")
	}
	result := copyConnection(c)
	e.mu.Unlock()
	return result, nil
}

func (e *Engine) submit(request Request) (*Job, bool, error) {
	if request.IdempotencyKey == "" || len(request.IdempotencyKey) > 4096 {
		return nil, false, errors.New("idempotency_key is required (maximum 4096 bytes)")
	}
	if strings.ContainsRune(request.Command, 0) {
		return nil, false, errors.New("command cannot contain NUL bytes")
	}
	if len(request.Command) > MaxCommandBytes {
		return nil, false, errors.New("command exceeds 1 MiB")
	}
	timeout := e.config.CommandTimeout.Milliseconds()
	if request.TimeoutMillis != 0 {
		timeout = request.TimeoutMillis
	}
	if timeout <= 0 || timeout > e.config.CommandTimeout.Milliseconds() {
		return nil, false, errors.New("timeout_ms must be positive and no greater than the runner command timeout")
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if err := e.check(); err != nil {
		return nil, false, err
	}
	for _, j := range e.db.Jobs {
		if j.Owner == request.Owner && j.IdempotencyKey == request.IdempotencyKey {
			if j.ConnectionID != request.ConnectionID || j.Command != request.Command || j.TimeoutMillis != timeout {
				return nil, false, ErrConflict
			}
			return copyJob(j), true, nil
		}
	}
	c, err := e.connection(request.Owner, request.ConnectionID)
	if err != nil {
		return nil, false, err
	}
	if c.State != Active {
		return nil, false, ErrLost
	}
	if c.BusyJob != "" {
		return nil, false, ErrBusy
	}
	active := 0
	for _, j := range e.db.Jobs {
		if !terminal(j.State) {
			active++
		}
	}
	if active >= 16 {
		return nil, false, ErrCapacity
	}
	if len(e.db.Jobs) >= e.config.MaxRecords {
		return nil, false, errors.New("retained job quota reached; retry after retention expiry")
	}
	j := &Job{ID: newID(), ConnectionID: c.ID, Owner: request.Owner, IdempotencyKey: request.IdempotencyKey, Command: request.Command, TimeoutMillis: timeout, State: Accepted, AcceptedAt: time.Now().UTC()}
	e.db.Jobs[j.ID] = j
	c.BusyJob, c.LastActivity = j.ID, j.AcceptedAt
	if err := e.persist(); err != nil {
		return nil, false, err
	}
	accepted := copyJob(j)
	e.jobs.Go(func() { e.execute(j.ID) })
	return accepted, false, nil
}

func (e *Engine) execute(id string) {
	e.mu.Lock()
	j := e.db.Jobs[id]
	if j == nil || terminal(j.State) || e.closing {
		e.mu.Unlock()
		return
	}
	if j.CancelReason != "" {
		c := e.db.Connections[j.ConnectionID]
		state := Failed
		if j.CancelReason == "cancelled" {
			state = Cancelled
		}
		j.State, j.Reason, j.ShellStateLost, j.FinishedAt = state, j.CancelReason, true, time.Now().UTC()
		c.State, c.Reason, c.BusyJob = Lost, j.CancelReason, ""
		e.persist()
		e.mu.Unlock()
		if err := stopShell(e.supervisor, j.ConnectionID); err == nil {
			e.mu.Lock()
			c.StopPending = false
			e.persist()
			e.mu.Unlock()
		}
		return
	}
	j.State, j.StartedAt = Running, time.Now().UTC()
	if err := e.persist(); err != nil {
		e.mu.Unlock()
		return
	}
	request := workerRequest{Action: "run", JobID: j.ID, Command: j.Command, TimeoutMillis: j.TimeoutMillis}
	socket := workerSocket(e.config, j.ConnectionID)
	e.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(j.TimeoutMillis)*time.Millisecond+e.config.CancelGrace+20*time.Second)
	defer cancel()
	var result workerResult
	err := Call(ctx, socket, request, &result)
	if err == nil {
		err = workerError(result)
	}
	if err != nil {
		result = workerResult{State: Lost, Reason: "shell_rpc_failed", Lost: true}
		if stopErr := stopShell(e.supervisor, j.ConnectionID); stopErr != nil {
			result.Reason = "shell_stop_failed"
		}
	}
	stopped := false
	if result.Lost {
		if stopErr := stopShell(e.supervisor, j.ConnectionID); stopErr != nil {
			result.State, result.Reason = Lost, "shell_stop_failed"
		} else {
			stopped = true
		}
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if terminal(j.State) {
		return
	}
	j.State, j.Reason, j.ExitCode, j.ShellStateLost, j.FinishedAt = result.State, result.Reason, result.ExitCode, result.Lost, time.Now().UTC()
	c := e.db.Connections[j.ConnectionID]
	c.BusyJob, c.LastActivity = "", j.FinishedAt
	if result.Lost {
		c.State, c.Reason = Lost, result.Reason
		c.StopPending = !stopped
	}
	e.persist()
}

func (e *Engine) findJob(request Request) *Job {
	if request.Action == "lookup" {
		for _, j := range e.db.Jobs {
			if j.Owner == request.Owner && j.IdempotencyKey == request.IdempotencyKey {
				return j
			}
		}
		return nil
	}
	j := e.db.Jobs[request.JobID]
	if j != nil && j.Owner == request.Owner {
		return j
	}
	return nil
}

func (e *Engine) lookup(request Request) Response {
	e.mu.Lock()
	defer e.mu.Unlock()
	if err := e.check(); err != nil {
		return responseError(err)
	}
	j := e.findJob(request)
	if j == nil {
		return responseError(ErrNotFound)
	}
	response := Response{Job: copyJob(j)}
	if request.Action != "output" {
		return response
	}
	limit := request.Limit
	if limit == 0 {
		limit = MaxChunk
	}
	if limit < 1 || limit > MaxChunk || request.StdoutCursor < 0 || request.StderrCursor < 0 {
		return responseError(errors.New("invalid output cursor or limit (1..65536 bytes per stream)"))
	}
	path := filepath.Join(e.config.Root, "connections", j.ConnectionID, "jobs", j.ID)
	stdout, nextOut, eofOut, err := readOutput(filepath.Join(path, "stdout"), request.StdoutCursor, limit)
	if err != nil {
		return responseError(err)
	}
	stderr, nextErr, eofErr, err := readOutput(filepath.Join(path, "stderr"), request.StderrCursor, limit)
	if err != nil {
		return responseError(err)
	}
	_, outDone := os.Stat(filepath.Join(path, "stdout.done"))
	_, errDone := os.Stat(filepath.Join(path, "stderr.done"))
	response.Output = &Output{StdoutFinal: outDone == nil, StderrFinal: errDone == nil, Stdout: stdout, Stderr: stderr, StdoutCursor: nextOut, StderrCursor: nextErr, StdoutEOF: eofOut, StderrEOF: eofErr}
	return response
}

func readOutput(path string, cursor int64, limit int) ([]byte, int64, bool, error) {
	f, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) && cursor == 0 {
		return []byte{}, 0, true, nil
	}
	if err != nil {
		return nil, cursor, false, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, cursor, false, err
	}
	if cursor > info.Size() {
		return nil, cursor, false, errors.New("output cursor exceeds captured stream size")
	}
	data := make([]byte, min(int64(limit), info.Size()-cursor))
	n, err := f.ReadAt(data, cursor)
	if err != nil && !errors.Is(err, io.EOF) {
		return nil, cursor, false, err
	}
	next := cursor + int64(n)
	return data[:n], next, next == info.Size(), nil
}

func (e *Engine) cancel(ctx context.Context, request Request, reason string) Response {
	e.mu.Lock()
	if err := e.check(); err != nil {
		e.mu.Unlock()
		return responseError(err)
	}
	j := e.findJob(request)
	if j == nil {
		e.mu.Unlock()
		return responseError(ErrNotFound)
	}
	if terminal(j.State) {
		response := Response{Job: copyJob(j)}
		e.mu.Unlock()
		return response
	}
	j.CancelReason = reason
	if err := e.persist(); err != nil {
		e.mu.Unlock()
		return responseError(err)
	}
	id, socket := j.ID, workerSocket(e.config, j.ConnectionID)
	e.mu.Unlock()
	cancelCtx, stop := context.WithTimeout(ctx, 5*time.Second)
	defer stop()
	for {
		var result workerResult
		err := Call(cancelCtx, socket, workerRequest{Action: "cancel", JobID: id, Reason: reason}, &result)
		if err == nil && result.State == Cancelled {
			break
		}
		e.mu.Lock()
		finished := terminal(j.State)
		e.mu.Unlock()
		if finished {
			break
		}
		select {
		case <-cancelCtx.Done():
			return responseError(errors.New("cancel acknowledgement uncertain; look up the job before retrying"))
		case <-time.After(20 * time.Millisecond):
		}
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	return Response{Job: copyJob(j)}
}

func (e *Engine) disconnect(request Request) Response {
	e.mu.Lock()
	if err := e.check(); err != nil {
		e.mu.Unlock()
		return responseError(err)
	}
	c, err := e.connection(request.Owner, request.ConnectionID)
	if err != nil {
		e.mu.Unlock()
		return responseError(err)
	}
	if c.State != Active && !c.StopPending {
		response := Response{Connection: copyConnection(c)}
		e.mu.Unlock()
		return response
	}
	c.State, c.Reason, c.LastActivity = Closed, "disconnected", time.Now().UTC()
	if j := e.db.Jobs[c.BusyJob]; j != nil && !terminal(j.State) {
		j.State, j.Reason, j.ShellStateLost, j.FinishedAt = Lost, "disconnected", true, time.Now().UTC()
	}
	c.BusyJob = ""
	if err := e.persist(); err != nil {
		e.mu.Unlock()
		return responseError(err)
	}
	response := Response{Connection: copyConnection(c)}
	e.mu.Unlock()
	if err := stopShell(e.supervisor, c.ID); err != nil {
		return responseError(err)
	}
	e.mu.Lock()
	c.StopPending = false
	saveErr := e.persist()
	e.mu.Unlock()
	if saveErr != nil {
		return responseError(saveErr)
	}
	response.Connection.StopPending = false
	return response
}

// Reap enforces idle/lifetime limits and retention independently of clients.
func (e *Engine) Reap(now time.Time) {
	type expired struct {
		id     string
		job    *Job
		reason string
	}
	var expiredShells []expired
	e.mu.Lock()
	if e.check() != nil {
		e.mu.Unlock()
		return
	}
	changed := false
	for _, c := range e.db.Connections {
		if c.State != Active {
			continue
		}
		reason := ""
		if now.Sub(c.CreatedAt) >= e.config.MaxLifetime {
			reason = "max_lifetime"
		} else if c.BusyJob == "" && now.Sub(c.LastActivity) >= e.config.IdleTimeout {
			reason = "idle_timeout"
		}
		if reason == "" {
			continue
		}
		var job *Job
		if j := e.db.Jobs[c.BusyJob]; j != nil && !terminal(j.State) {
			j.CancelReason = reason
			job = copyJob(j)
		}
		expiredShells = append(expiredShells, expired{c.ID, job, reason})
		c.State, c.Reason = Lost, reason
		changed = true
	}
	for id, j := range e.db.Jobs {
		if terminal(j.State) && now.Sub(j.FinishedAt) >= e.config.ResultTTL {
			if err := os.RemoveAll(filepath.Join(e.config.Root, "connections", j.ConnectionID, "jobs", id)); err != nil {
				e.fatal = err
				e.mu.Unlock()
				return
			}
			delete(e.db.Jobs, id)
			changed = true
		}
	}
	for id, c := range e.db.Connections {
		if c.State == Active || c.StopPending || c.BusyJob != "" || now.Sub(c.LastActivity) < e.config.ResultTTL {
			continue
		}
		hasJobs := false
		for _, j := range e.db.Jobs {
			if j.ConnectionID == id {
				hasJobs = true
				break
			}
		}
		if !hasJobs {
			if err := os.RemoveAll(filepath.Join(e.config.Root, "connections", id)); err != nil {
				e.fatal = err
				e.mu.Unlock()
				return
			}
			os.Remove(workerSocket(e.config, id))
			delete(e.db.Connections, id)
			changed = true
		}
	}
	if changed {
		e.persist()
	}
	e.mu.Unlock()
	// Slow systemd stops must not hold the engine lock: another connection's
	// status, output, or independent cancel remains available during the grace.
	for _, shell := range expiredShells {
		if shell.job != nil {
			result := e.cancel(context.Background(), Request{Action: "cancel", Owner: shell.job.Owner, JobID: shell.job.ID}, shell.reason)
			if result.Error != "" {
				stopErr := stopShell(e.supervisor, shell.id)
				e.mu.Lock()
				if stopErr != nil {
					e.fatal = stopErr
				} else if j := e.db.Jobs[shell.job.ID]; j != nil && !terminal(j.State) {
					j.State, j.Reason, j.ShellStateLost, j.FinishedAt = Failed, shell.reason, true, time.Now().UTC()
					c := e.db.Connections[shell.id]
					c.BusyJob, c.StopPending = "", false
					e.persist()
				}
				e.mu.Unlock()
			}
		} else if err := stopShell(e.supervisor, shell.id); err != nil {
			e.mu.Lock()
			e.fatal = err
			e.mu.Unlock()
		} else {
			e.mu.Lock()
			if c := e.db.Connections[shell.id]; c != nil {
				c.StopPending = false
				e.persist()
			}
			e.mu.Unlock()
		}
	}
}

func (e *Engine) Serve(ctx context.Context) error {
	listener, err := listen(e.config.Socket)
	if err != nil {
		return err
	}
	defer listener.Close()
	reapCtx, stop := context.WithCancel(ctx)
	defer stop()
	go func() {
		ticker := time.NewTicker(e.config.ReapInterval)
		defer ticker.Stop()
		for {
			select {
			case now := <-ticker.C:
				e.Reap(now)
			case <-reapCtx.Done():
				return
			}
		}
	}()
	return serve(ctx, listener, func(data json.RawMessage) any {
		var request Request
		if err := decode(data, &request); err != nil {
			return responseError(err)
		}
		return e.Handle(context.Background(), request)
	})
}

func (e *Engine) Close() error {
	e.mu.Lock()
	e.closing = true
	var ids []string
	for _, c := range e.db.Connections {
		if c.State != Active && !c.StopPending {
			continue
		}
		ids = append(ids, c.ID)
		c.State, c.Reason, c.BusyJob = Lost, "runner_stopped", ""
	}
	for _, j := range e.db.Jobs {
		if !terminal(j.State) {
			j.State, j.Reason, j.ShellStateLost, j.FinishedAt = Lost, "runner_stopped", true, time.Now().UTC()
		}
	}
	var failures []error
	if err := e.persist(); err != nil {
		failures = append(failures, err)
	}
	e.mu.Unlock()
	var stops sync.WaitGroup
	var stopsMu sync.Mutex
	for _, id := range ids {
		stops.Go(func() {
			if err := stopShell(e.supervisor, id); err != nil {
				stopsMu.Lock()
				failures = append(failures, err)
				stopsMu.Unlock()
				return
			}
			e.mu.Lock()
			e.db.Connections[id].StopPending = false
			if err := e.persist(); err != nil {
				stopsMu.Lock()
				failures = append(failures, err)
				stopsMu.Unlock()
			}
			e.mu.Unlock()
		})
	}
	stops.Wait()
	e.jobs.Wait()
	failures = append(failures, e.store.close())
	return errors.Join(failures...)
}
