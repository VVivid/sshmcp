package gateway

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"encoding/pem"
	"io"
	"net"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"

	"sshmcp/internal/runner"
)

// This loopback SSH fixture uses the actual multiplexed transport. Ordinary
// calls stay blocked until released so control and sibling cancellation can be
// checked independently of scheduler timing or a real target VM.
func TestSSHWaitCancellationPreservesSiblingAndControlRPCs(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	started := make(chan struct{}, 6)
	release := make(chan struct{})
	releaseAll := sync.OnceFunc(func() { close(release) })
	defer releaseAll()
	rpc := newTestSSHRunner(t, func(incoming ssh.NewChannel) {
		channel, requests, err := incoming.Accept()
		if err != nil {
			return
		}
		defer channel.Close()
		for request := range requests {
			if request.Type != "exec" {
				request.Reply(false, nil)
				continue
			}
			request.Reply(true, nil)
			var input runner.Request
			if json.NewDecoder(channel).Decode(&input) != nil {
				return
			}
			if input.Action == "output" {
				started <- struct{}{}
				select {
				case <-release:
				case <-ctx.Done():
					return
				}
			}
			json.NewEncoder(channel).Encode(runner.Response{Job: &runner.Job{ID: input.JobID, State: runner.Completed}})
			channel.SendRequest("exit-status", false, ssh.Marshal(struct{ Status uint32 }{0}))
			return
		}
	})
	firstCtx, cancelFirst := context.WithCancel(ctx)
	defer cancelFirst()
	first := make(chan error, 1)
	go func() {
		_, err := rpc.Call(firstCtx, runner.Request{Action: "output", JobID: "cancelled-wait"})
		first <- err
	}()
	select {
	case <-started:
	case <-ctx.Done():
		t.Fatal("first data RPC did not start")
	}
	var siblings sync.WaitGroup
	for range 5 {
		siblings.Go(func() {
			if result, err := rpc.Call(ctx, runner.Request{Action: "output", JobID: "sibling"}); err != nil || result.Job == nil || result.Job.State != runner.Completed {
				t.Errorf("cancelling one wait dropped a sibling RPC: %+v, %v", result, err)
			}
		})
	}
	for range 5 {
		select {
		case <-started:
		case <-ctx.Done():
			t.Fatal("six ordinary data RPCs did not overlap")
		}
	}
	controlCtx, stopControl := context.WithTimeout(ctx, time.Second)
	defer stopControl()
	if result, err := rpc.Call(controlCtx, runner.Request{Action: "cancel", JobID: "control"}); err != nil || result.Job == nil {
		t.Fatalf("saturated data slots blocked control: %+v, %v", result, err)
	}
	cancelFirst()
	select {
	case err := <-first:
		if err == nil {
			t.Fatal("cancelled read returned false success")
		}
	case <-ctx.Done():
		t.Fatal("RPC cancellation did not close its session")
	}
	releaseAll()
	siblings.Wait()
}

func TestSSHChannelOpenCancellationPreservesTransportAndBoundsCleanup(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	var opens atomic.Int32
	opening, started, abandonedClosed := make(chan struct{}), make(chan struct{}), make(chan struct{})
	release := make(chan struct{})
	releaseAll := sync.OnceFunc(func() { close(release) })
	defer releaseAll()
	rpc := newTestSSHRunner(t, func(incoming ssh.NewChannel) {
		abandoned := opens.Add(1) == 2
		if abandoned {
			close(opening)
			select {
			case <-release:
			case <-ctx.Done():
				return
			}
		}
		channel, requests, err := incoming.Accept()
		if err != nil {
			return
		}
		defer channel.Close()
		for request := range requests {
			if abandoned {
				t.Error("cancelled channel open dispatched a command after returning")
				return
			}
			request.Reply(true, nil)
			var input runner.Request
			if json.NewDecoder(channel).Decode(&input) != nil {
				return
			}
			if input.JobID == "sibling" {
				close(started)
				select {
				case <-release:
				case <-ctx.Done():
					return
				}
			}
			json.NewEncoder(channel).Encode(runner.Response{Job: &runner.Job{ID: input.JobID, State: runner.Completed}})
			channel.SendRequest("exit-status", false, ssh.Marshal(struct{ Status uint32 }{0}))
			return
		}
		if abandoned {
			close(abandonedClosed)
		}
	})
	sibling := make(chan error, 1)
	go func() {
		_, err := rpc.Call(ctx, runner.Request{Action: "output", JobID: "sibling"})
		sibling <- err
	}()
	select {
	case <-started:
	case <-ctx.Done():
		t.Fatal("sibling did not start")
	}
	openCtx, cancelOpen := context.WithCancel(ctx)
	defer cancelOpen()
	result := make(chan error, 1)
	go func() {
		_, err := rpc.Call(openCtx, runner.Request{Action: "output", JobID: "abandoned"})
		result <- err
	}()
	select {
	case <-opening:
	case <-ctx.Done():
		t.Fatal("second channel open did not reach the SSH server")
	}
	cancelOpen()
	select {
	case err := <-result:
		if err == nil {
			t.Fatal("cancelled channel open returned success")
		}
	case <-ctx.Done():
		t.Fatal("channel open ignored caller cancellation")
	}
	if got := len(rpc.data.slots); got != 2 {
		t.Errorf("abandoned open must retain its slot until cleanup: got %d occupied slots, want 2", got)
	}
	for _, action := range []string{"output", "cancel"} {
		if response, err := rpc.Call(ctx, runner.Request{Action: action, JobID: "healthy"}); err != nil || response.Job == nil {
			t.Errorf("channel-open cancellation broke a sibling %s RPC: %+v, %v", action, response, err)
		}
	}
	releaseAll()
	select {
	case err := <-sibling:
		if err != nil {
			t.Fatalf("channel-open cancellation dropped an active sibling: %v", err)
		}
	case <-ctx.Done():
		t.Fatal("sibling did not complete")
	}
	select {
	case <-abandonedClosed:
	case <-ctx.Done():
		t.Fatal("abandoned channel was not closed after opening")
	}
}

func TestSSHWaitCancellationDuringContendedReconnect(t *testing.T) {
	rpc := newTestSSHRunner(t, func(incoming ssh.NewChannel) {
		incoming.Reject(ssh.Prohibited, "cancelled requests must not dispatch")
	})
	// A reconnect holds this mutex while dialing and negotiating SSH. A sibling
	// caller's optional wait must still be able to expire before that finishes.
	rpc.data.mu.Lock()
	unlock := sync.OnceFunc(rpc.data.mu.Unlock)
	defer unlock()
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	returned := make(chan error, 1)
	go func() {
		_, err := rpc.Call(ctx, runner.Request{Action: "output", JobID: "reconnecting"})
		returned <- err
	}()
	started := time.After(time.Second)
	for len(rpc.data.slots) == 0 {
		select {
		case <-started:
			t.Fatal("reconnecting caller did not acquire its bounded slot")
		case <-time.After(time.Millisecond):
		}
	}
	cancel()
	select {
	case err := <-returned:
		if err == nil {
			t.Fatal("cancelled reconnect returned success")
		}
	case <-time.After(time.Second):
		t.Fatal("optional wait remained blocked on another caller's reconnect")
	}
	if got := len(rpc.data.slots); got != 1 {
		t.Fatalf("pending reconnect must retain its slot until cleanup: got %d occupied slots, want 1", got)
	}
	unlock()
	deadline := time.After(time.Second)
	for len(rpc.data.slots) != 0 {
		select {
		case <-deadline:
			t.Fatal("cancelled reconnect did not release its slot after the mutex became available")
		case <-time.After(time.Millisecond):
		}
	}
}

func TestSSHWaitReturnsWhilePeerIgnoresChannelClose(t *testing.T) {
	rpc := newTestSSHRunner(t, func(incoming ssh.NewChannel) {
		channel, requests, err := incoming.Accept()
		if err != nil {
			return
		}
		defer channel.Close()
		for request := range requests {
			request.Reply(true, nil)
			var input runner.Request
			if json.NewDecoder(channel).Decode(&input) != nil {
				return
			}
			json.NewEncoder(channel).Encode(runner.Response{Job: &runner.Job{ID: input.JobID, State: runner.Completed}})
			channel.SendRequest("exit-status", false, ssh.Marshal(struct{ Status uint32 }{0}))
			return
		}
	})
	client, err := rpc.data.connection(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	stalled := &unresponsiveSSHChannel{done: make(chan struct{}), requests: make(chan *ssh.Request)}
	stalled.release = sync.OnceFunc(func() { close(stalled.done); close(stalled.requests) })
	defer stalled.release()
	// The synthetic first channel accepts exec, then withholds output, exit
	// status and close acknowledgement. Sibling channels use the real SSH peer.
	channels, requests := make(chan ssh.NewChannel), make(chan *ssh.Request)
	close(channels)
	close(requests)
	rpc.data.mu.Lock()
	rpc.data.client = ssh.NewClient(&unresponsiveChannelConn{Conn: client.Conn, stalled: stalled}, channels, requests)
	rpc.data.mu.Unlock()
	s := server{rpc: rpc}
	acknowledged := runner.Response{Job: &runner.Job{ID: "acknowledged", State: runner.Running}}
	before := time.Now()
	type waitResult struct {
		response runner.Response
		err      error
	}
	returned := make(chan waitResult, 1)
	go func() {
		response, err := s.wait(t.Context(), runner.Request{Action: "run"}, acknowledged, 50*time.Millisecond)
		returned <- waitResult{response, err}
	}()
	select {
	case result := <-returned:
		if result.err != nil || result.response.Job.ID != acknowledged.Job.ID || time.Since(before) > time.Second {
			t.Fatalf("unresponsive SSH channel lost acknowledgement: %+v, %v", result.response, result.err)
		}
	case <-time.After(time.Second):
		t.Fatal("unresponsive SSH channel overran the bounded wait")
	}
	if got := len(rpc.data.slots); got != 1 {
		t.Fatalf("unresponsive RPC must retain its slot during cleanup: got %d occupied slots, want 1", got)
	}
	ctx, cancel := context.WithTimeout(t.Context(), time.Second)
	defer cancel()
	for _, action := range []string{"output", "cancel"} {
		if response, err := rpc.Call(ctx, runner.Request{Action: action, JobID: "healthy"}); err != nil || response.Job == nil {
			t.Errorf("bounded wait interrupted a healthy sibling %s RPC: %+v, %v", action, response, err)
		}
	}
	stalled.release()
	for len(rpc.data.slots) != 0 {
		select {
		case <-ctx.Done():
			t.Fatal("recovered RPC did not finish physical cleanup and release its slot")
		case <-time.After(time.Millisecond):
		}
	}
}

type unresponsiveChannelConn struct {
	ssh.Conn
	stalled *unresponsiveSSHChannel
	opened  atomic.Bool
}

func (c *unresponsiveChannelConn) OpenChannel(kind string, extra []byte) (ssh.Channel, <-chan *ssh.Request, error) {
	if !c.opened.Swap(true) {
		return c.stalled, c.stalled.requests, nil
	}
	return c.Conn.OpenChannel(kind, extra)
}

func (c *unresponsiveChannelConn) Close() error {
	c.stalled.release()
	return c.Conn.Close()
}

type unresponsiveSSHChannel struct {
	done     chan struct{}
	requests chan *ssh.Request
	release  func()
}

func (c *unresponsiveSSHChannel) Read([]byte) (int, error)  { <-c.done; return 0, io.EOF }
func (*unresponsiveSSHChannel) Write(p []byte) (int, error) { return len(p), nil }
func (*unresponsiveSSHChannel) Close() error                { return nil }
func (*unresponsiveSSHChannel) CloseWrite() error           { return nil }
func (*unresponsiveSSHChannel) SendRequest(string, bool, []byte) (bool, error) {
	return true, nil
}
func (c *unresponsiveSSHChannel) Stderr() io.ReadWriter { return c }

func newTestSSHRunner(t *testing.T, handle func(ssh.NewChannel)) *SSHRunner {
	t.Helper()
	_, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	signer, err := ssh.NewSignerFromKey(private)
	if err != nil {
		t.Fatal(err)
	}
	config := &ssh.ServerConfig{NoClientAuth: true}
	config.AddHostKey(signer)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			go func() {
				server, channels, requests, err := ssh.NewServerConn(conn, config)
				if err != nil {
					conn.Close()
					return
				}
				defer server.Close()
				go ssh.DiscardRequests(requests)
				for incoming := range channels {
					go handle(incoming)
				}
			}()
		}
	}()
	block, err := ssh.MarshalPrivateKey(private, "local test")
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "key")
	if err := os.WriteFile(path, pem.EncodeToMemory(block), 0o600); err != nil {
		t.Fatal(err)
	}
	rpc, err := NewSSHRunner(SSHConfig{Address: listener.Addr().String(), User: "local-test", PrivateKey: path, HostKey: string(ssh.MarshalAuthorizedKey(signer.PublicKey()))})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { rpc.Close() })
	return rpc
}
