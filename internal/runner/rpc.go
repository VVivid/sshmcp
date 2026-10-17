package runner

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// Call makes one short Unix-socket RPC. Closing the caller's connection never
// cancels an accepted job; cancellation is a separate, explicit request.
func Call(ctx context.Context, socket string, request, response any) error {
	conn, err := (&net.Dialer{}).DialContext(ctx, "unix", socket)
	if err != nil {
		return err
	}
	defer conn.Close()
	stop := context.AfterFunc(ctx, func() { conn.Close() })
	defer stop()
	if deadline, ok := ctx.Deadline(); ok {
		conn.SetDeadline(deadline)
	}
	if err := json.NewEncoder(conn).Encode(request); err != nil {
		return err
	}
	return json.NewDecoder(io.LimitReader(conn, MaxResponseBytes)).Decode(response)
}

func listen(socket string) (net.Listener, error) {
	if err := os.MkdirAll(filepath.Dir(socket), 0o700); err != nil {
		return nil, err
	}
	if info, err := os.Lstat(socket); err == nil {
		if info.Mode()&os.ModeSocket == 0 {
			return nil, errors.New("refusing to replace a non-socket path")
		}
		if err := os.Remove(socket); err != nil {
			return nil, err
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	listener, err := net.Listen("unix", socket)
	if err != nil {
		return nil, err
	}
	if err := os.Chmod(socket, 0o600); err != nil {
		listener.Close()
		return nil, err
	}
	return listener, nil
}

func serve(ctx context.Context, listener net.Listener, handle func(json.RawMessage) any) error {
	var handlers sync.WaitGroup
	defer handlers.Wait()
	stop := context.AfterFunc(ctx, func() { listener.Close() })
	defer stop()
	for {
		conn, err := listener.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		handlers.Go(func() {
			defer conn.Close()
			conn.SetReadDeadline(time.Now().Add(10 * time.Second))
			var request json.RawMessage
			if err := json.NewDecoder(io.LimitReader(conn, MaxRequestBytes)).Decode(&request); err != nil {
				json.NewEncoder(conn).Encode(Response{Error: "invalid JSON request", Code: "invalid_request"})
				return
			}
			conn.SetReadDeadline(time.Time{})
			response := handle(request)
			conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
			json.NewEncoder(conn).Encode(response)
		})
	}
}

func decode(data []byte, value any) error {
	// A single decoded object is required. Unknown fields catch misspelled
	// ownership, timeout, and cursor fields instead of silently ignoring them.
	reader := bytes.NewReader(data)
	decoder := json.NewDecoder(reader)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(value); err != nil {
		return fmt.Errorf("invalid request: %w", err)
	}
	return nil
}
