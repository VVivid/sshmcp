package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"sync"
	"time"

	"golang.org/x/crypto/ssh"

	"sshmcp/internal/runner"
)

const rpcCommand = "XDG_RUNTIME_DIR=/run/user/$(id -u) /usr/local/bin/sshmcp-runner rpc"

// SSHConfig is administrator configuration. Tools can select only lab-target;
// they cannot supply a network address, SSH user, key, or command prefix.
type SSHConfig struct {
	Address    string `json:"address"`
	User       string `json:"user"`
	PrivateKey string `json:"private_key"`
	HostKey    string `json:"host_key"`
}

// SSHRunner keeps two bounded transports: six ordinary RPC channels and two
// independent control channels. Both stay below OpenSSH's default MaxSessions
// of ten. No job holds a channel after its short durable acknowledgement.
type SSHRunner struct {
	data    *sshTransport
	control *sshTransport
}

type sshTransport struct {
	mu      sync.Mutex
	client  *ssh.Client
	config  *ssh.ClientConfig
	address string
	slots   chan struct{}
	closed  bool
}

func NewSSHRunner(config SSHConfig) (*SSHRunner, error) {
	if _, _, err := net.SplitHostPort(config.Address); err != nil || config.User == "" {
		return nil, errors.New("SSH address must include a port and user is required")
	}
	key, err := os.ReadFile(config.PrivateKey)
	if err != nil {
		return nil, fmt.Errorf("read gateway SSH key: %w", err)
	}
	signer, err := ssh.ParsePrivateKey(key)
	if err != nil {
		return nil, errors.New("invalid gateway SSH private key")
	}
	pin, _, _, rest, err := ssh.ParseAuthorizedKey([]byte(config.HostKey))
	if err != nil || len(bytes.TrimSpace(rest)) != 0 || pin.Type() != ssh.KeyAlgoED25519 {
		return nil, errors.New("one pinned Ed25519 target host key is required")
	}
	clientConfig := &ssh.ClientConfig{
		User:              config.User,
		Auth:              []ssh.AuthMethod{ssh.PublicKeys(signer)},
		HostKeyCallback:   ssh.FixedHostKey(pin),
		HostKeyAlgorithms: []string{ssh.KeyAlgoED25519},
		Timeout:           8 * time.Second,
	}
	return &SSHRunner{
		data: &sshTransport{
			config:  clientConfig,
			address: config.Address,
			slots:   make(chan struct{}, 6),
		},
		control: &sshTransport{
			config:  clientConfig,
			address: config.Address,
			slots:   make(chan struct{}, 2),
		},
	}, nil
}

func (r *SSHRunner) Close() error { return errors.Join(r.data.close(), r.control.close()) }

func (t *sshTransport) close() error {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.closed = true
	if t.client != nil {
		return t.client.Close()
	}
	return nil
}

func (t *sshTransport) connection(ctx context.Context) (*ssh.Client, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if t.closed {
		return nil, errors.New("gateway is shutting down")
	}
	if t.client != nil {
		return t.client, nil
	}
	conn, err := (&net.Dialer{Timeout: 8 * time.Second}).DialContext(ctx, "tcp", t.address)
	if err != nil {
		return nil, err
	}
	conn.SetDeadline(time.Now().Add(8 * time.Second))
	stop := context.AfterFunc(ctx, func() { conn.Close() })
	c, channels, requests, err := ssh.NewClientConn(conn, t.address, t.config)
	cancellationStopped := stop()
	if err != nil {
		conn.Close()
		return nil, err
	}
	// Do not publish a client whose cancellation callback already owns conn.Close.
	if !cancellationStopped {
		conn.Close()
		return nil, ctx.Err()
	}
	conn.SetDeadline(time.Time{})
	client := ssh.NewClient(c, channels, requests)
	t.client = client
	go func() {
		client.Wait()
		t.mu.Lock()
		defer t.mu.Unlock()
		if t.client == client {
			t.client = nil
		}
	}()
	return client, nil
}

// Call never retries a dispatched RPC. A dropped acknowledgement could mean the
// target already accepted execution; only an explicit lookup can resolve it.
func (r *SSHRunner) Call(ctx context.Context, request runner.Request) (runner.Response, error) {
	t := r.data
	if request.Action == "cancel" || request.Action == "disconnect" {
		t = r.control
	}
	select {
	case t.slots <- struct{}{}:
	case <-ctx.Done():
		return runner.Response{}, ctx.Err()
	}
	type callResult struct {
		response runner.Response
		err      error
	}
	finished := make(chan callResult)
	// Keep the slot until physical cleanup finishes, even after the caller's
	// bounded wait has returned. Pending reconnects, opens and RPCs stay bounded
	// by the existing data/control capacities.
	go func() {
		defer func() { <-t.slots }()
		response, err := t.call(ctx, request)
		select {
		case finished <- callResult{response, err}:
		case <-ctx.Done():
		}
	}()
	select {
	case result := <-finished:
		return result.response, result.err
	case <-ctx.Done():
		return runner.Response{}, ctx.Err()
	}
}

func (t *sshTransport) call(ctx context.Context, request runner.Request) (runner.Response, error) {
	client, err := t.connection(ctx)
	if err != nil {
		return runner.Response{}, err
	}
	// A caller deadline closes only its own channel. A peer that never answers
	// channel open/close still cannot hold a slot forever: this independent
	// physical RPC deadline retires the unresponsive transport after 30 seconds.
	watchdog := time.AfterFunc(30*time.Second, func() { client.Close() })
	defer watchdog.Stop()
	session, err := client.NewSession()
	if err != nil {
		return runner.Response{}, err
	}
	stopSession := context.AfterFunc(ctx, func() { session.Close() })
	defer stopSession()
	defer session.Close()
	if err := ctx.Err(); err != nil {
		return runner.Response{}, err
	}
	payload, err := json.Marshal(request)
	if err != nil {
		return runner.Response{}, err
	}
	session.Stdin = bytes.NewReader(append(payload, '\n'))
	var out boundedBuffer
	out.remaining = runner.MaxResponseBytes
	session.Stdout = &out
	// Target error strings can contain private data. They are neither logged nor
	// returned; the typed JSON response is the only accepted result.
	session.Stderr = io.Discard
	runErr := session.Run(rpcCommand)
	var response runner.Response
	if out.overflow || json.Unmarshal(out.Bytes(), &response) != nil {
		if err := ctx.Err(); err != nil {
			return runner.Response{}, err
		}
		return runner.Response{}, errors.New("target RPC response unavailable or oversized; acknowledgement uncertain")
	}
	if runErr != nil && response.Error == "" {
		if err := ctx.Err(); err != nil {
			return runner.Response{}, err
		}
		return runner.Response{}, errors.New("SSH RPC acknowledgement uncertain")
	}
	return response, nil
}

type boundedBuffer struct {
	bytes.Buffer
	remaining int
	overflow  bool
}

func (b *boundedBuffer) Write(p []byte) (int, error) {
	n := len(p)
	if n > b.remaining {
		b.overflow = true
		p = p[:b.remaining]
	}
	b.Buffer.Write(p)
	b.remaining -= len(p)
	return n, nil
}
