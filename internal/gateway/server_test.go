package gateway

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"encoding/pem"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/go-jose/go-jose/v4"
	"github.com/modelcontextprotocol/go-sdk/auth"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"golang.org/x/crypto/ssh"

	"sshmcp/internal/runner"
)

func TestRunInput(t *testing.T) {
	id := strings.Repeat("a", 32)
	valid := []RunInput{
		{Action: "execute", ConnectionID: id, IdempotencyKey: "once", Command: "printf hello"},
		{Action: "execute", ConnectionID: id, IdempotencyKey: "batch", Commands: []string{"cd /tmp", "pwd"}},
		{Action: "status", JobID: id, StdoutCursor: 4, Limit: 1},
		{Action: "status", ConnectionID: id},
		{Action: "lookup", IdempotencyKey: "once"},
		{Action: "cancel", JobID: id},
		{Action: "execute", ConnectionID: id, IdempotencyKey: "wait", Command: "true", WaitMillis: 10000, Limit: 3},
		{Action: "status", JobID: id, WaitMillis: 10000, StdoutCursor: 4, Limit: 1},
	}
	for _, in := range valid {
		if _, err := in.request(); err != nil {
			t.Errorf("valid input rejected: %#v: %v", in, err)
		}
	}
	invalid := []RunInput{
		{Action: "execute", ConnectionID: id, Command: "true"},
		{Action: "execute", ConnectionID: id, IdempotencyKey: "k", Command: "x", Commands: []string{"y"}},
		{Action: "execute", ConnectionID: id, IdempotencyKey: "k", Command: "x\x00"},
		{Action: "execute", ConnectionID: id, IdempotencyKey: "k", Command: strings.Repeat("x", runner.MaxCommandBytes+1)},
		{Action: "execute", ConnectionID: id, IdempotencyKey: "k", Command: "x", TimeoutMillis: 3600001},
		{Action: "status", JobID: id, ConnectionID: id},
		{Action: "status", JobID: id, Command: "x"},
		{Action: "status", JobID: id, StdoutCursor: -1},
		{Action: "status", JobID: id, Limit: 16385},
		{Action: "lookup", IdempotencyKey: "k", JobID: id},
		{Action: "cancel", JobID: "bad"},
		{Action: "run", ConnectionID: id, IdempotencyKey: "k", Command: "true"},
		{Action: "execute", ConnectionID: id, IdempotencyKey: "k", Command: "true", WaitMillis: -1},
		{Action: "execute", ConnectionID: id, IdempotencyKey: "k", Command: "true", WaitMillis: 10001},
		{Action: "execute", ConnectionID: id, IdempotencyKey: "k", Command: "true", Limit: 3},
		{Action: "status", JobID: id, WaitMillis: -1},
		{Action: "status", JobID: id, WaitMillis: 10001},
		{Action: "status", ConnectionID: id, WaitMillis: 1},
		{Action: "lookup", IdempotencyKey: "k", WaitMillis: 1},
		{Action: "cancel", JobID: id, WaitMillis: 1},
	}
	for _, in := range invalid {
		if _, err := in.request(); err == nil {
			t.Errorf("invalid input accepted: %#v", in)
		}
	}
	batch, _ := valid[1].request()
	if batch.Command != "cd /tmp\npwd" || batch.Action != "run" {
		t.Fatal(batch)
	}
}

type recordingRPC struct {
	requests []runner.Request
	response runner.Response
	err      error
}

func (r *recordingRPC) Call(_ context.Context, in runner.Request) (runner.Response, error) {
	r.requests = append(r.requests, in)
	return r.response, r.err
}

func TestVerifiedOwnershipAndNoReplay(t *testing.T) {
	rpc := &recordingRPC{err: errors.New("transport dropped")}
	s := server{rpc: rpc}
	owner := runner.Owner{Issuer: "verified-issuer", Subject: "verified-subject"}
	req := &mcp.CallToolRequest{Extra: &mcp.RequestExtra{TokenInfo: &auth.TokenInfo{Extra: map[string]any{"owner": owner}}}}
	result, _, err := s.run(t.Context(), req, RunInput{Action: "execute", ConnectionID: strings.Repeat("a", 32), IdempotencyKey: "durable-key", Command: "expensive"})
	if err != nil || !result.IsError || len(rpc.requests) != 1 || rpc.requests[0].Owner != owner {
		t.Fatalf("unexpected execute: %v %#v %#v", err, result, rpc.requests)
	}
	if !strings.Contains(result.Content[0].(*mcp.TextContent).Text, "SAME") {
		t.Fatal("uncertainty must give lookup instruction")
	}
	result, _, _ = s.run(t.Context(), &mcp.CallToolRequest{}, RunInput{Action: "lookup", IdempotencyKey: "durable-key"})
	if !result.IsError || len(rpc.requests) != 1 {
		t.Fatal("unauthenticated call reached target")
	}
	result, _, _ = s.connect(t.Context(), req, ConnectInput{HostAlias: "192.168.1.1"})
	if !result.IsError || len(rpc.requests) != 1 {
		t.Fatal("arbitrary host reached target")
	}
}

func TestPublicOutputIsBoundedAndLossless(t *testing.T) {
	response := publicResponse(runner.Response{Job: &runner.Job{ID: "job", Command: strings.Repeat("secret-source", 10000), Owner: runner.Owner{Subject: "private"}}, Output: &runner.Output{Stdout: []byte("hello\n"), Stderr: []byte{0, 255}, StdoutCursor: 6, StderrCursor: 2}})
	encoded, _ := json.Marshal(response)
	if strings.Contains(string(encoded), "secret-source") || strings.Contains(string(encoded), "private") {
		t.Fatal("command or identity leaked")
	}
	streams := response["output"].(map[string]any)
	if streams["stdout"].(map[string]any)["text"] != "hello\n" || streams["stderr"].(map[string]any)["base64"] != "AP8=" {
		t.Fatal(response)
	}
	if publicStream([]byte{0xe2, 0x82}, 2, true, false)["encoding"] != "base64" {
		t.Fatal("partial UTF-8 must preserve bytes")
	}
	for _, reason := range []string{"shell_completion_missing", "shell_control_corrupted"} {
		job := publicResponse(runner.Response{Job: &runner.Job{State: runner.Lost, Reason: reason, ShellStateLost: true}})["job"].(map[string]any)
		diagnostic, _ := job["diagnostic"].(string)
		if job["exit_code"] != (*int)(nil) || !strings.Contains(diagnostic, "never replay") || !strings.Contains(diagnostic, "new connection") {
			t.Fatalf("lost completion is not actionable or invented an exit code: %+v", job)
		}
		if reason == "shell_control_corrupted" && (!strings.Contains(diagnostic, "oversized") || !strings.Contains(diagnostic, "read reliably") || strings.Contains(diagnostic, "Bash exited")) {
			t.Fatalf("control read failure was misdiagnosed as shell exit or EOF: %q", diagnostic)
		}
	}
}

func TestStatelessToolCatalog(t *testing.T) {
	config := Config{Listen: "127.0.0.1:18080", Resource: "http://127.0.0.1:18080/mcp", Issuer: "http://127.0.0.1:18081/realms/sshmcp"}
	verifier := func(context.Context, string, *http.Request) (*auth.TokenInfo, error) {
		return &auth.TokenInfo{Scopes: []string{AdminScope}, Expiration: time.Now().Add(time.Hour), Extra: map[string]any{"owner": runner.Owner{Issuer: config.Issuer, Subject: "account"}}}, nil
	}
	handler, err := NewHandler(config, &recordingRPC{}, verifier)
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest("POST", config.Resource, strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}`))
	request.Header.Set("Authorization", "Bearer test")
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json, text/event-stream")
	request.Header.Set("MCP-Protocol-Version", "2026-07-28")
	request.Header.Set("Mcp-Method", "tools/list")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != 200 {
		t.Fatalf("HTTP %d: %s", response.Code, response.Body.String())
	}
	var result struct {
		Result struct {
			Tools []struct {
				Name string `json:"name"`
			} `json:"tools"`
		} `json:"result"`
	}
	if json.Unmarshal(response.Body.Bytes(), &result) != nil || len(result.Result.Tools) != 3 {
		t.Fatal(response.Body.String())
	}
	rpc := &recordingRPC{response: runner.Response{Connection: &runner.Connection{ID: strings.Repeat("a", 32), State: runner.Active}}}
	handler, err = NewHandler(config, rpc, verifier)
	if err != nil {
		t.Fatal(err)
	}
	request = httptest.NewRequest("POST", config.Resource, strings.NewReader(`{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"connect","arguments":{"host_alias":"lab-target"},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}`))
	request.Header.Set("Authorization", "Bearer test")
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json, text/event-stream")
	request.Header.Set("MCP-Protocol-Version", "2026-07-28")
	request.Header.Set("Mcp-Method", "tools/call")
	request.Header.Set("Mcp-Name", "connect")
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != 200 || len(rpc.requests) != 1 || rpc.requests[0].Owner.Subject != "account" || !strings.Contains(response.Body.String(), "connection_id") {
		t.Fatalf("HTTP tool call failed: %d %s %#v", response.Code, response.Body.String(), rpc.requests)
	}
	// Default Go JSON encoding expands > to six bytes. The largest legal source
	// must cross both HTTP body readers, including the MCP SDK's separate limit.
	large := RunInput{Action: "execute", ConnectionID: strings.Repeat("a", 32), IdempotencyKey: strings.Repeat(">", 4096), Command: "#" + strings.Repeat(">", runner.MaxCommandBytes-1)}
	payload, err := json.Marshal(map[string]any{
		"jsonrpc": "2.0", "id": 3, "method": "tools/call",
		"params": map[string]any{
			"name": "run_command", "arguments": large,
			"_meta": map[string]any{"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": map[string]any{}},
		},
	})
	if err != nil || len(payload) <= 4<<20 || len(payload) > runner.MaxRequestBytes {
		t.Fatalf("escaping fixture outside expected request bounds: bytes=%d, err=%v", len(payload), err)
	}
	request = httptest.NewRequest("POST", config.Resource, bytes.NewReader(payload))
	request.Header.Set("Authorization", "Bearer test")
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json, text/event-stream")
	request.Header.Set("MCP-Protocol-Version", "2026-07-28")
	request.Header.Set("Mcp-Method", "tools/call")
	request.Header.Set("Mcp-Name", "run_command")
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != 200 || len(rpc.requests) != 2 || rpc.requests[1].Command != large.Command {
		t.Fatalf("maximum escaped HTTP source was not forwarded intact: HTTP %d, RPC calls=%d", response.Code, len(rpc.requests))
	}
}

func TestNativeSSHPinAndControlCapacity(t *testing.T) {
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	block, err := ssh.MarshalPrivateKey(private, "test")
	if err != nil {
		t.Fatal(err)
	}
	keyPath := filepath.Join(t.TempDir(), "key")
	if err := os.WriteFile(keyPath, pem.EncodeToMemory(block), 0o600); err != nil {
		t.Fatal(err)
	}
	pin, _ := ssh.NewPublicKey(public)
	r, err := NewSSHRunner(SSHConfig{Address: "127.0.0.1:22", User: "test", PrivateKey: keyPath, HostKey: string(ssh.MarshalAuthorizedKey(pin))})
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	if cap(r.data.slots) != 6 || cap(r.control.slots) != 2 || r.data == r.control {
		t.Fatal("control capacity must be independent and below MaxSessions")
	}
	other, _, _ := ed25519.GenerateKey(rand.Reader)
	otherKey, _ := ssh.NewPublicKey(other)
	if r.data.config.HostKeyCallback("host", nil, pin) != nil || r.data.config.HostKeyCallback("host", nil, otherKey) == nil {
		t.Fatal("host pin not enforced")
	}
}

func TestJWTVerificationAndHTTPAuthorization(t *testing.T) {
	// The production verifier intentionally accepts Keycloak's RS256 only.
	// Use an RSA fixture to exercise its actual discovery and JWKS validation.
	key := testRSAKey(t)
	var issuer string
	idp := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if strings.HasSuffix(r.URL.Path, "/certs") {
			json.NewEncoder(w).Encode(jose.JSONWebKeySet{Keys: []jose.JSONWebKey{{Key: &key.PublicKey, KeyID: "test", Algorithm: "RS256", Use: "sig"}}})
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"issuer": issuer, "jwks_uri": issuer + "/certs", "authorization_endpoint": issuer + "/auth", "token_endpoint": issuer + "/token"})
	}))
	defer idp.Close()
	issuer = idp.URL + "/realms/sshmcp"
	verifier, err := NewTokenVerifier(t.Context(), issuer, "http://127.0.0.1:18080/mcp")
	if err != nil {
		t.Fatal(err)
	}
	base := map[string]any{"iss": issuer, "sub": "account-a", "aud": "http://127.0.0.1:18080/mcp", "exp": time.Now().Add(time.Hour).Unix(), "scope": AdminScope}
	for _, tc := range []struct {
		name, key string
		value     any
		want      int
	}{
		{"valid", "", nil, 200}, {"issuer", "iss", "https://wrong.invalid", 401}, {"audience", "aud", "elsewhere", 401}, {"expired", "exp", time.Now().Add(-time.Hour).Unix(), 401}, {"missing-exp", "exp", nil, 401}, {"missing-sub", "sub", "", 401}, {"scope", "scope", "other", 403},
		{"bad-signature", "", nil, 401},
	} {
		t.Run(tc.name, func(t *testing.T) {
			claims := map[string]any{}
			for k, v := range base {
				claims[k] = v
			}
			if tc.key != "" {
				if tc.value == nil {
					delete(claims, tc.key)
				} else {
					claims[tc.key] = tc.value
				}
			}
			signingKey := key
			if tc.name == "bad-signature" {
				signingKey = testRSAKey(t)
			}
			signer, err := jose.NewSigner(jose.SigningKey{Algorithm: jose.RS256, Key: signingKey}, (&jose.SignerOptions{}).WithHeader("kid", "test"))
			if err != nil {
				t.Fatal(err)
			}
			payload, _ := json.Marshal(claims)
			signed, err := signer.Sign(payload)
			if err != nil {
				t.Fatal(err)
			}
			token, err := signed.CompactSerialize()
			if err != nil {
				t.Fatal(err)
			}
			handler := auth.RequireBearerToken(verifier, &auth.RequireBearerTokenOptions{Scopes: []string{AdminScope}})(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				owner, err := verifiedOwner(auth.TokenInfoFromContext(r.Context()))
				if err != nil || owner != (runner.Owner{Issuer: issuer, Subject: "account-a"}) {
					t.Error("incorrect verified owner")
				}
				w.WriteHeader(200)
			}))
			req := httptest.NewRequest("POST", "http://127.0.0.1:18080/mcp", nil)
			req.Header.Set("Authorization", "Bearer "+token)
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, req)
			if rec.Code != tc.want {
				t.Fatalf("status %d, want %d: %s", rec.Code, tc.want, rec.Body.String())
			}
		})
	}
}
