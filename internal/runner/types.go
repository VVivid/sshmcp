// Package runner provides target-side persistent shells and durable job records.
// The Unix socket is an administrator interface, not an authorization boundary
// against the target account or root. A future gateway supplies verified owners.
package runner

import (
	"encoding/json"
	"errors"
	"time"
)

const (
	Accepted  = "accepted"
	Running   = "running"
	Completed = "completed"
	Failed    = "failed"
	Cancelled = "cancelled"
	Lost      = "lost"
	Active    = "active"
	Closed    = "closed"
	MaxChunk  = 64 << 10
)

const (
	MaxCommandBytes = 1 << 20
	// JSON may encode one source byte as six bytes (for example, > as \u003e).
	// The extra 128 KiB covers the three bounded 4096-byte owner/key fields,
	// their own worst-case escaping, and the RPC or MCP envelope.
	MaxRequestBytes  = 6*MaxCommandBytes + (128 << 10)
	MaxResponseBytes = 2 << 20
)

type Owner struct {
	Issuer  string `json:"issuer"`
	Subject string `json:"subject"`
}

func (o Owner) valid() bool {
	return o.Issuer != "" && o.Subject != "" && len(o.Issuer) <= 4096 && len(o.Subject) <= 4096
}

type Connection struct {
	ID           string    `json:"id"`
	Owner        Owner     `json:"owner"`
	State        string    `json:"state"`
	Reason       string    `json:"reason,omitempty"`
	CreatedAt    time.Time `json:"created_at"`
	LastActivity time.Time `json:"last_activity"`
	BusyJob      string    `json:"busy_job,omitempty"`
	// StopPending records the obligation to stop a possibly live shell. It stays
	// true while active and clears only after a confirmed stop, so recovery also
	// retries cleanup for closed or lost connections.
	StopPending bool `json:"shell_stop_pending,omitzero"`
}

type Job struct {
	ID             string    `json:"id"`
	ConnectionID   string    `json:"connection_id"`
	Owner          Owner     `json:"owner"`
	IdempotencyKey string    `json:"idempotency_key"`
	Command        string    `json:"command,omitempty"`
	TimeoutMillis  int64     `json:"timeout_ms"`
	State          string    `json:"state"`
	Reason         string    `json:"reason,omitempty"`
	ExitCode       *int      `json:"exit_code,omitempty"`
	AcceptedAt     time.Time `json:"accepted_at"`
	StartedAt      time.Time `json:"started_at,omitzero"`
	FinishedAt     time.Time `json:"finished_at,omitzero"`
	CancelReason   string    `json:"cancel_reason,omitempty"`
	ShellStateLost bool      `json:"shell_state_lost,omitzero"`
}

func terminal(state string) bool {
	return state != Accepted && state != Running
}

type Request struct {
	Action         string `json:"action"`
	Owner          Owner  `json:"owner"`
	ConnectionID   string `json:"connection_id,omitempty"`
	JobID          string `json:"job_id,omitempty"`
	IdempotencyKey string `json:"idempotency_key,omitempty"`
	Command        string `json:"command,omitempty"`
	TimeoutMillis  int64  `json:"timeout_ms,omitzero"`
	StdoutCursor   int64  `json:"stdout_cursor,omitzero"`
	StderrCursor   int64  `json:"stderr_cursor,omitzero"`
	Limit          int    `json:"limit,omitzero"`
}

type Output struct {
	// JSON encodes these byte slices as base64, preserving arbitrary output.
	Stdout       []byte `json:"stdout"`
	Stderr       []byte `json:"stderr"`
	StdoutCursor int64  `json:"stdout_cursor"`
	StderrCursor int64  `json:"stderr_cursor"`
	StdoutEOF    bool   `json:"stdout_eof"`
	StderrEOF    bool   `json:"stderr_eof"`
	StdoutFinal  bool   `json:"stdout_final"`
	StderrFinal  bool   `json:"stderr_final"`
}

type Response struct {
	Connection *Connection `json:"connection,omitempty"`
	Job        *Job        `json:"job,omitempty"`
	Output     *Output     `json:"output,omitempty"`
	Duplicate  bool        `json:"duplicate,omitzero"`
	Error      string      `json:"error,omitempty"`
	Code       string      `json:"code,omitempty"`
}

// MarshalJSON projects job metadata for RPC responses. The complete source stays
// in the durable Job and internal execution/idempotency paths, but must not grow
// every acknowledgement or output response. Copy before removing it so encoding
// a response can never change the stored job or another caller's view.
func (r Response) MarshalJSON() ([]byte, error) {
	type wireResponse Response
	result := wireResponse(r)
	if r.Job != nil {
		job := *r.Job
		job.Command = ""
		result.Job = &job
	}
	return json.Marshal(result)
}

var (
	ErrNotFound = errors.New("not found for this owner")
	ErrConflict = errors.New("idempotency key already has a different request")
	ErrBusy     = errors.New("connection already has an accepted or running job")
	ErrCapacity = errors.New("the runner already has 16 accepted or running jobs")
	ErrLost     = errors.New("connection shell state is unavailable; open a new connection")
)

type Config struct {
	Root           string
	Socket         string
	Executable     string
	Home           string
	IdleTimeout    time.Duration
	CommandTimeout time.Duration
	MaxLifetime    time.Duration
	ResultTTL      time.Duration
	CancelGrace    time.Duration
	ReapInterval   time.Duration
	OutputLimit    int64
	MaxRecords     int
}

func (c Config) withDefaults() Config {
	if c.IdleTimeout == 0 {
		c.IdleTimeout = 15 * time.Minute
	}
	if c.CommandTimeout == 0 {
		c.CommandTimeout = time.Hour
	}
	if c.MaxLifetime == 0 {
		c.MaxLifetime = 24 * time.Hour
	}
	if c.ResultTTL == 0 {
		c.ResultTTL = 24 * time.Hour
	}
	if c.CancelGrace == 0 {
		c.CancelGrace = 10 * time.Second
	}
	if c.ReapInterval == 0 {
		c.ReapInterval = time.Second
	}
	if c.OutputLimit == 0 {
		c.OutputLimit = 8 << 20
	}
	if c.MaxRecords == 0 {
		c.MaxRecords = 256
	}
	return c
}
