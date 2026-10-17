package gateway

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/modelcontextprotocol/go-sdk/auth"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/modelcontextprotocol/go-sdk/oauthex"

	"sshmcp/internal/runner"
)

type RPC interface {
	Call(context.Context, runner.Request) (runner.Response, error)
}

type Config struct {
	Listen   string    `json:"listen"`
	Issuer   string    `json:"issuer"`
	Resource string    `json:"resource"`
	SSH      SSHConfig `json:"ssh"`
}

type ConnectInput struct {
	HostAlias string `json:"host_alias" jsonschema:"Configured host alias. Only lab-target is allowed."`
}
type DisconnectInput struct {
	ConnectionID string `json:"connection_id" jsonschema:"Application connection ID returned by connect."`
}
type RunInput struct {
	Action         string   `json:"action" jsonschema:"execute, status, lookup, or cancel"`
	ConnectionID   string   `json:"connection_id,omitempty" jsonschema:"Required for execute; status may inspect this connection instead of a job."`
	JobID          string   `json:"job_id,omitempty" jsonschema:"Required for job status or cancel. Cancellation closes the persistent shell and loses its state."`
	IdempotencyKey string   `json:"idempotency_key,omitempty" jsonschema:"Required for execute and lookup. Save this key before execution. Reuse unchanged to deduplicate."`
	Command        string   `json:"command,omitempty" jsonschema:"Bash source for execute. Supply command or commands, never both."`
	Commands       []string `json:"commands,omitempty" jsonschema:"Serial newline-separated Bash batch in one job and persistent shell. Final command determines exit status."`
	TimeoutMillis  int64    `json:"timeout_ms,omitzero" jsonschema:"Execution deadline in milliseconds, 1 through 3600000. Default 3600000."`
	StdoutCursor   int64    `json:"stdout_cursor,omitzero" jsonschema:"Byte cursor from the previous status response; default zero."`
	StderrCursor   int64    `json:"stderr_cursor,omitzero" jsonschema:"Byte cursor from the previous status response; default zero."`
	Limit          int      `json:"limit,omitzero" jsonschema:"Maximum output bytes per stream for status, 1 through 16384; default 8192."`
}

type server struct{ rpc RPC }

// NewHandler registers exactly three tools. Stateless HTTP does not own shells;
// connection and job IDs remain usable by the same verified principal after a
// gateway restart or a new MCP client connection.
func NewHandler(config Config, rpc RPC, verifier auth.TokenVerifier) (http.Handler, error) {
	resource, err := url.Parse(config.Resource)
	if err != nil || resource.Scheme != "http" || resource.Host != config.Listen ||
		resource.Hostname() != "127.0.0.1" || resource.Path != "/mcp" ||
		resource.RawQuery != "" || resource.Fragment != "" {
		return nil, errors.New("resource must be http://127.0.0.1:<listen-port>/mcp")
	}
	issuer, err := url.Parse(config.Issuer)
	if err != nil || issuer.Scheme != "http" || issuer.Hostname() != "127.0.0.1" ||
		issuer.Path != "/realms/sshmcp" {
		return nil, errors.New("issuer must be the private loopback sshmcp realm")
	}
	s := &server{rpc: rpc}
	m := mcp.NewServer(&mcp.Implementation{Name: "sshmcp", Version: "0.5.0"}, &mcp.ServerOptions{Instructions: "Use connect(lab-target) for an independent persistent Bash shell. Save each execute idempotency_key before submission. Poll status with job_id and byte cursors; never wait by resubmitting a command. On uncertain acknowledgement, use lookup with the SAME key. SSH/gateway loss does not cancel jobs. Disconnect closes the managed shell. Shell and retained job results expire; lost jobs are never replayed. This is a private administrative lab."})
	mcp.AddTool(m, &mcp.Tool{
		Name:        "connect",
		Description: "Open an independent persistent Bash shell on configured lab-target. Returns an application connection_id; HTTP transport sessions do not own it.",
	}, s.connect)
	mcp.AddTool(m, &mcp.Tool{
		Name:        "run_command",
		Description: "Execute Bash source or a serial batch asynchronously, inspect incremental output/status, lookup an idempotency key, or cancel a job. Exactly one active job per connection; sixteen across the target. Accepted execution is durable and is never automatically replayed.",
	}, s.run)
	mcp.AddTool(m, &mcp.Tool{
		Name:        "disconnect",
		Description: "Close a managed connection and terminate its shell and active job. Retained job status/output remains available to its owner.",
	}, s.disconnect)
	transport := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return m }, &mcp.StreamableHTTPOptions{
		Stateless:           true,
		JSONResponse:        true,
		MaxRequestBodyBytes: runner.MaxRequestBytes,
	})
	metadataURL := resource.Scheme + "://" + resource.Host + "/.well-known/oauth-protected-resource/mcp"
	protected := auth.RequireBearerToken(verifier, &auth.RequireBearerTokenOptions{
		ResourceMetadataURL: metadataURL,
		Scopes:              []string{AdminScope},
	})(transport)
	mux := http.NewServeMux()
	metadata := auth.ProtectedResourceMetadataHandler(&oauthex.ProtectedResourceMetadata{
		Resource:               config.Resource,
		AuthorizationServers:   []string{config.Issuer},
		ScopesSupported:        []string{AdminScope},
		BearerMethodsSupported: []string{"header"},
	})
	mux.Handle("/.well-known/oauth-protected-resource/mcp", metadata)
	mux.Handle("/.well-known/oauth-protected-resource", metadata)
	mux.Handle("/mcp", protected)
	slots := make(chan struct{}, 64)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Host != resource.Host {
			http.Error(w, "unexpected Host", http.StatusBadRequest)
			return
		}
		if origin := r.Header.Get("Origin"); origin != "" && origin != resource.Scheme+"://"+resource.Host {
			http.Error(w, "unexpected Origin", http.StatusForbidden)
			return
		}
		select {
		case slots <- struct{}{}:
			defer func() { <-slots }()
		default:
			http.Error(w, "gateway capacity reached; retry later", http.StatusTooManyRequests)
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, runner.MaxRequestBytes)
		mux.ServeHTTP(w, r)
	}), nil
}

func toolError(code, message string) (*mcp.CallToolResult, any, error) {
	data := map[string]any{"code": code, "error": message}
	encoded, _ := json.Marshal(data)
	return &mcp.CallToolResult{
		IsError:           true,
		Content:           []mcp.Content{&mcp.TextContent{Text: string(encoded)}},
		StructuredContent: data,
	}, nil, nil
}

func (s *server) invoke(ctx context.Context, req *mcp.CallToolRequest, request runner.Request) (*mcp.CallToolResult, any, error) {
	if req.Extra == nil {
		return toolError("unauthenticated", "verified identity unavailable")
	}
	owner, err := verifiedOwner(req.Extra.TokenInfo)
	if err != nil {
		return toolError("unauthenticated", err.Error())
	}
	request.Owner = owner
	rpcCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	response, err := s.rpc.Call(rpcCtx, request)
	if err != nil {
		message := "Target RPC acknowledgement uncertain. No automatic retry was attempted. Inspect target readiness and retry a read-only status or lookup."
		if request.Action == "run" {
			message = "Execution acknowledgement uncertain. NEVER submit a new key. Use run_command action=lookup with the SAME idempotency_key; if absent after target recovery, retry execute only with the exact same key, connection, command and timeout. A lost job must not be replayed."
		}
		return toolError("acknowledgement_uncertain", message)
	}
	if response.Error != "" {
		return toolError(response.Code, response.Error)
	}
	result := publicResponse(response)
	encoded, _ := json.Marshal(result)
	return &mcp.CallToolResult{
		Content:           []mcp.Content{&mcp.TextContent{Text: string(encoded)}},
		StructuredContent: result,
	}, nil, nil
}

func (s *server) connect(ctx context.Context, req *mcp.CallToolRequest, in ConnectInput) (*mcp.CallToolResult, any, error) {
	if in.HostAlias != "lab-target" {
		return toolError("invalid_request", "host_alias must be lab-target; arbitrary hosts are disabled")
	}
	return s.invoke(ctx, req, runner.Request{Action: "connect"})
}

func (s *server) disconnect(ctx context.Context, req *mcp.CallToolRequest, in DisconnectInput) (*mcp.CallToolResult, any, error) {
	if !validID(in.ConnectionID) {
		return toolError("invalid_request", "a valid connection_id is required")
	}
	return s.invoke(ctx, req, runner.Request{Action: "disconnect", ConnectionID: in.ConnectionID})
}

func validID(id string) bool {
	if len(id) != 32 {
		return false
	}
	for _, c := range id {
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
			return false
		}
	}
	return true
}

func (in RunInput) request() (runner.Request, error) {
	r := runner.Request{
		ConnectionID:   in.ConnectionID,
		JobID:          in.JobID,
		IdempotencyKey: in.IdempotencyKey,
		TimeoutMillis:  in.TimeoutMillis,
		StdoutCursor:   in.StdoutCursor,
		StderrCursor:   in.StderrCursor,
		Limit:          in.Limit,
	}
	if in.StdoutCursor < 0 || in.StderrCursor < 0 || in.Limit < 0 || in.Limit > 16384 {
		return r, errors.New("cursors must be nonnegative; limit must be 1..16384 (or omitted)")
	}
	if in.ConnectionID != "" && !validID(in.ConnectionID) ||
		in.JobID != "" && !validID(in.JobID) || len(in.IdempotencyKey) > 4096 {
		return r, errors.New("invalid connection/job ID or idempotency key")
	}
	if in.Action != "execute" && (in.Command != "" || len(in.Commands) > 0 || in.TimeoutMillis != 0) {
		return r, errors.New("command, commands and timeout_ms belong only to execute")
	}
	if in.Action != "status" && (in.StdoutCursor != 0 || in.StderrCursor != 0 || in.Limit != 0) {
		return r, errors.New("output cursors and limit belong only to status")
	}
	switch in.Action {
	case "execute":
		if !validID(in.ConnectionID) || in.IdempotencyKey == "" || in.JobID != "" {
			return r, errors.New("execute requires connection_id and idempotency_key; job_id is not accepted")
		}
		if (in.Command == "") == (len(in.Commands) == 0) || len(in.Commands) > 64 {
			return r, errors.New("execute requires command OR a serial commands batch (maximum 64 items)")
		}
		r.Command = in.Command
		if len(in.Commands) > 0 {
			for _, command := range in.Commands {
				if command == "" {
					return r, errors.New("batch commands cannot be empty")
				}
			}
			r.Command = strings.Join(in.Commands, "\n")
		}
		if len(r.Command) > runner.MaxCommandBytes || strings.ContainsRune(r.Command, 0) ||
			in.TimeoutMillis < 0 || in.TimeoutMillis > 3600000 {
			return r, errors.New("command must be at most 1 MiB without NUL; timeout_ms must be 1..3600000 (or omitted)")
		}
		r.Action = "run"
	case "status":
		if in.IdempotencyKey != "" || (in.ConnectionID == "") == (in.JobID == "") {
			return r, errors.New("status requires exactly one job_id or connection_id")
		}
		if in.JobID != "" {
			r.Action = "output"
			if r.Limit == 0 {
				r.Limit = 8192
			}
		} else {
			if in.Limit != 0 || in.StdoutCursor != 0 || in.StderrCursor != 0 {
				return r, errors.New("connection status does not accept output cursors")
			}
			r.Action = "connection"
		}
	case "lookup":
		if in.IdempotencyKey == "" || in.ConnectionID != "" || in.JobID != "" {
			return r, errors.New("lookup requires only the original idempotency_key")
		}
		r.Action = "lookup"
	case "cancel":
		if !validID(in.JobID) || in.ConnectionID != "" || in.IdempotencyKey != "" {
			return r, errors.New("cancel requires only job_id")
		}
		r.Action = "cancel"
	default:
		return r, fmt.Errorf("action must be execute, status, lookup, or cancel")
	}
	return r, nil
}

func (s *server) run(ctx context.Context, req *mcp.CallToolRequest, in RunInput) (*mcp.CallToolResult, any, error) {
	request, err := in.request()
	if err != nil {
		return toolError("invalid_request", err.Error())
	}
	return s.invoke(ctx, req, request)
}

// Public responses omit owner claims and original command source. Polling a
// large script must not echo that script in every response.
func publicResponse(response runner.Response) map[string]any {
	out := map[string]any{}
	if c := response.Connection; c != nil {
		out["connection"] = map[string]any{
			"connection_id": c.ID,
			"state":         c.State,
			"reason":        c.Reason,
			"busy_job_id":   c.BusyJob,
			"created_at":    c.CreatedAt,
			"last_activity": c.LastActivity,
		}
	}
	if j := response.Job; j != nil {
		out["job"] = map[string]any{
			"job_id":           j.ID,
			"connection_id":    j.ConnectionID,
			"state":            j.State,
			"reason":           j.Reason,
			"exit_code":        j.ExitCode,
			"shell_state_lost": j.ShellStateLost,
			"accepted_at":      j.AcceptedAt,
			"started_at":       j.StartedAt,
			"finished_at":      j.FinishedAt,
		}
	}
	if response.Duplicate {
		out["duplicate"] = true
	}
	if o := response.Output; o != nil {
		out["output"] = map[string]any{
			"stdout": publicStream(o.Stdout, o.StdoutCursor, o.StdoutEOF, o.StdoutFinal),
			"stderr": publicStream(o.Stderr, o.StderrCursor, o.StderrEOF, o.StderrFinal),
		}
	}
	return out
}

func publicStream(data []byte, cursor int64, eof, final bool) map[string]any {
	out := map[string]any{"next_cursor": cursor, "eof": eof, "final": final}
	text := utf8.Valid(data)
	for _, b := range data {
		if b < 32 && b != '\n' && b != '\r' && b != '\t' || b == 127 {
			text = false
			break
		}
	}
	if text {
		out["encoding"] = "utf-8"
		out["text"] = string(data)
	} else {
		out["encoding"] = "base64"
		out["base64"] = base64.StdEncoding.EncodeToString(data)
	}
	return out
}
