package runner

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

type workerRequest struct {
	Action        string `json:"action"`
	JobID         string `json:"job_id,omitempty"`
	Command       string `json:"command,omitempty"`
	TimeoutMillis int64  `json:"timeout_ms,omitzero"`
	Reason        string `json:"reason,omitempty"`
}

type workerResult struct {
	State    string `json:"state"`
	Reason   string `json:"reason,omitempty"`
	ExitCode *int   `json:"exit_code,omitempty"`
	Lost     bool   `json:"lost,omitzero"`
	Error    string `json:"error,omitempty"`
}

// Shell diagnostics include failures outside a job's temporary redirections.
// They have a separate small cap so DEBUG traps cannot bypass output quotas.
type boundedLog struct {
	mu        sync.Mutex
	file      *os.File
	remaining int
}

func (l *boundedLog) Write(data []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	keep := min(len(data), l.remaining)
	if keep > 0 {
		if _, err := l.file.Write(data[:keep]); err != nil {
			return 0, err
		}
		l.remaining -= keep
	}
	return len(data), nil
}

type execution struct {
	id     string
	ctx    context.Context
	cancel context.CancelCauseFunc
}

// Each event carries a record or read error. Clean EOF closes the channel without
// an error event, keeping it distinct from malformed or oversized data.
type shellCompletion struct {
	line string
	err  error
}

type shellWorker struct {
	mu          sync.Mutex
	root        string
	shellInput  io.WriteCloser
	process     *os.Process
	shellExited chan struct{}
	completion  chan shellCompletion
	current     *execution
	grace       time.Duration
	outputLimit int64
}

// RunWorker is invoked by a dedicated systemd user service for each connection.
// Bash has a separate process group; the service cgroup also catches descendants
// that change groups. Detached Docker containers are owned by Docker's service.
func RunWorker(ctx context.Context, root, socket, home string, grace time.Duration, outputLimit int64) error {
	if err := os.MkdirAll(root, 0o700); err != nil {
		return err
	}
	diagnostic, err := os.OpenFile(filepath.Join(root, "shell.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	defer diagnostic.Close()
	controlRead, controlWrite, err := os.Pipe()
	if err != nil {
		return err
	}
	defer controlRead.Close()
	cmd := exec.Command("/bin/bash", "--noprofile", "--norc")
	cmd.Dir = home
	cmd.Env = append(os.Environ(), "HOME="+home)
	log := &boundedLog{file: diagnostic, remaining: 64 << 10}
	cmd.Stdout, cmd.Stderr = log, log
	cmd.WaitDelay = time.Second
	cmd.ExtraFiles = []*os.File{controlWrite}
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		controlWrite.Close()
		return err
	}
	if err := cmd.Start(); err != nil {
		controlWrite.Close()
		stdin.Close()
		return err
	}
	controlWrite.Close()
	w := &shellWorker{
		root:        root,
		shellInput:  stdin,
		process:     cmd.Process,
		shellExited: make(chan struct{}),
		completion:  make(chan shellCompletion, 1),
		grace:       grace,
		outputLimit: outputLimit,
	}
	defer w.kill()
	go func() { cmd.Wait(); close(w.shellExited) }()
	go func() {
		defer close(w.completion)
		scanner := bufio.NewScanner(controlRead)
		for scanner.Scan() {
			select {
			case w.completion <- shellCompletion{line: scanner.Text()}:
			case <-w.shellExited:
				return
			}
		}
		if err := scanner.Err(); err != nil {
			// A bounded-record failure is not EOF, even if Bash and FD 3 remain open.
			select {
			case w.completion <- shellCompletion{err: err}:
			case <-w.shellExited:
			}
		}
	}()
	listener, err := listen(socket)
	if err != nil {
		return err
	}
	defer listener.Close()
	workerCtx, stop := context.WithCancel(ctx)
	defer stop()
	go func() {
		select {
		case <-w.shellExited:
			stop()
		case <-workerCtx.Done():
		}
	}()
	return serve(workerCtx, listener, func(data json.RawMessage) any {
		var request workerRequest
		if err := decode(data, &request); err != nil {
			return workerResult{Error: err.Error()}
		}
		switch request.Action {
		case "ready":
			select {
			case <-w.shellExited:
				return workerResult{Error: "shell exited"}
			default:
				return workerResult{State: Active}
			}
		case "run":
			return w.run(request)
		case "cancel":
			w.mu.Lock()
			defer w.mu.Unlock()
			if w.current != nil && w.current.id == request.JobID {
				w.current.cancel(errors.New(request.Reason))
				return workerResult{State: Cancelled}
			}
			return workerResult{State: "idle"}
		default:
			return workerResult{Error: "unknown worker action"}
		}
	})
}

func shellQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", "'\\''") + "'" }

// capture uses a per-job FIFO so background writers cannot contaminate the next
// job's output. The anchor lets setup open the reader without blocking on Bash.
// After completion, closing it allows EOF when the command's writers close.
func (w *shellWorker) capture(path string, current *execution) (*os.File, <-chan struct{}, error) {
	fifo := path + ".pipe"
	if err := syscall.Mkfifo(fifo, 0o600); err != nil {
		return nil, nil, err
	}
	anchor, err := os.OpenFile(fifo, os.O_RDWR, 0o600)
	if err != nil {
		return nil, nil, err
	}
	reader, err := os.Open(fifo)
	if err != nil {
		anchor.Close()
		return nil, nil, err
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		anchor.Close()
		reader.Close()
		return nil, nil, err
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		defer reader.Close()
		defer file.Close()
		defer os.Remove(fifo)
		written := int64(0)
		buffer := make([]byte, 32<<10)
		for {
			n, readErr := reader.Read(buffer)
			if n > 0 {
				keep := min(int64(n), max(0, w.outputLimit-written))
				if keep > 0 {
					if _, err := file.Write(buffer[:keep]); err != nil {
						current.cancel(errors.New("output_write_failed"))
					}
					written += keep
				}
				if keep < int64(n) {
					current.cancel(errors.New("output_limit"))
				}
			}
			if readErr != nil {
				if !errors.Is(readErr, io.EOF) {
					current.cancel(errors.New("output_read_failed"))
				} else if err := file.Sync(); err != nil {
					current.cancel(errors.New("output_sync_failed"))
				} else if err := atomicFile(path+".done", []byte("done\n")); err != nil {
					current.cancel(errors.New("output_sync_failed"))
				}
				return
			}
		}
	}()
	return anchor, done, nil
}

func (w *shellWorker) run(request workerRequest) workerResult {
	if !validID(request.JobID) || request.TimeoutMillis <= 0 {
		return workerResult{Error: "invalid worker job"}
	}
	w.mu.Lock()
	if w.current != nil {
		w.mu.Unlock()
		return workerResult{Error: "shell is busy"}
	}
	select {
	case <-w.shellExited:
		w.mu.Unlock()
		return workerResult{State: Lost, Reason: "shell_exited", Lost: true}
	default:
	}
	ctx, cancel := context.WithCancelCause(context.Background())
	current := &execution{id: request.JobID, ctx: ctx, cancel: cancel}
	w.current = current
	w.mu.Unlock()
	defer func() { cancel(nil); w.mu.Lock(); w.current = nil; w.mu.Unlock() }()
	deadline := time.AfterFunc(time.Duration(request.TimeoutMillis)*time.Millisecond, func() { cancel(errors.New("command_timeout")) })
	defer deadline.Stop()
	jobDir := filepath.Join(w.root, "jobs", request.JobID)
	if err := os.MkdirAll(jobDir, 0o700); err != nil {
		return workerResult{Error: err.Error(), Lost: true}
	}
	stdoutPath := filepath.Join(jobDir, "stdout")
	stderrPath := filepath.Join(jobDir, "stderr")
	stdoutAnchor, stdoutDone, err := w.capture(stdoutPath, current)
	if err != nil {
		return workerResult{Error: err.Error(), Lost: true}
	}
	defer stdoutAnchor.Close()
	stderrAnchor, stderrDone, err := w.capture(stderrPath, current)
	if err != nil {
		return workerResult{Error: err.Error(), Lost: true}
	}
	defer stderrAnchor.Close()
	// eval runs in the existing Bash process, preserving cwd, variables and
	// functions. Commands receive /dev/null stdin, not the next control script.
	// The completion FD is independent of both output streams. If shell options,
	// traps, exit, or descriptor changes break it, the shell is lost, never replayed.
	token := newID()
	script := "builtin eval -- " + shellQuote(request.Command) +
		" </dev/null >" + shellQuote(stdoutPath+".pipe") +
		" 2>" + shellQuote(stderrPath+".pipe") + "\n" +
		"builtin printf '%s %s\\n' " + shellQuote(token) + " \"$?\" >&3\n"
	scriptWritten := make(chan error, 1)
	go func() { _, err := io.WriteString(w.shellInput, script); scriptWritten <- err }()
	select {
	case err := <-scriptWritten:
		if err != nil {
			w.kill()
			return workerResult{State: Lost, Reason: "shell_control_failed", Lost: true}
		}
	case <-ctx.Done():
		return w.interrupt(context.Cause(ctx).Error())
	case <-w.shellExited:
		return workerResult{State: Lost, Reason: "shell_completion_missing", Lost: true}
	}
	result := workerResult{}
	select {
	case completion, open := <-w.completion:
		if !open {
			// EOF can precede process.Wait when Bash exits, or the command can
			// close FD 3 while Bash stays alive. Neither supplies a command result.
			w.kill()
			result = workerResult{State: Lost, Reason: "shell_completion_missing", Lost: true}
			break
		}
		if completion.err != nil {
			w.kill()
			result = workerResult{State: Lost, Reason: "shell_control_corrupted", Lost: true}
			break
		}
		fields := strings.Fields(completion.line)
		if len(fields) != 2 || fields[0] != token {
			w.kill()
			result = workerResult{State: Lost, Reason: "shell_control_corrupted", Lost: true}
			break
		}
		code, err := strconv.Atoi(fields[1])
		if err != nil || code < 0 || code > 255 {
			w.kill()
			result = workerResult{State: Lost, Reason: "shell_control_corrupted", Lost: true}
			break
		}
		result = workerResult{State: Completed, ExitCode: new(code)}
		if code != 0 {
			result.State = Failed
		}
	case <-w.shellExited:
		result = workerResult{State: Lost, Reason: "shell_completion_missing", Lost: true}
	case <-ctx.Done():
		result = w.interrupt(context.Cause(ctx).Error())
	}
	// A quota failure can race the completion pipe. Give normal output readers
	// a bounded drain before reporting the outcome, then honor any quota error.
	stdoutAnchor.Close()
	stderrAnchor.Close()
	drain := time.NewTimer(250 * time.Millisecond)
	defer drain.Stop()
	for _, done := range []<-chan struct{}{stdoutDone, stderrDone} {
		select {
		case <-done:
		case <-drain.C:
			goto drained
		}
	}
drained:
	if ctx.Err() != nil && !result.Lost {
		result = w.interrupt(context.Cause(ctx).Error())
	}
	return result
}

func (w *shellWorker) interrupt(reason string) workerResult {
	// Cancellation always invalidates Bash state, even when the foreground
	// command responds promptly. Reusing a partly interrupted eval is unsafe.
	syscall.Kill(-w.process.Pid, syscall.SIGINT)
	timer := time.NewTimer(w.grace)
	defer timer.Stop()
	select {
	case <-w.shellExited:
	case <-timer.C:
	}
	w.kill()
	state := Failed
	if reason == "cancelled" {
		state = Cancelled
	}
	return workerResult{State: state, Reason: reason, Lost: true}
}

func (w *shellWorker) kill() {
	syscall.Kill(-w.process.Pid, syscall.SIGKILL)
	w.shellInput.Close()
}

func workerSocket(config Config, id string) string {
	return filepath.Join(filepath.Dir(config.Socket), "sshmcp-"+id+".sock")
}

func workerError(result workerResult) error {
	if result.Error != "" {
		return fmt.Errorf("shell worker: %s", result.Error)
	}
	return nil
}
