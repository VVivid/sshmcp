// sshmcp-runner is installed only on the execution target. rpc reads one JSON
// request from stdin, forwards it over the private local socket, and exits.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"sshmcp/internal/runner"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	if len(os.Args) < 2 {
		return errors.New("usage: sshmcp-runner daemon|rpc|shell-worker [options]")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	runtime := os.Getenv("XDG_RUNTIME_DIR")
	if runtime == "" {
		runtime = filepath.Join(home, ".sshmcp", "run")
	}
	set := flag.NewFlagSet(os.Args[1], flag.ContinueOnError)
	root := set.String("root", filepath.Join(home, ".sshmcp"), "private durable state directory")
	socket := set.String("socket", filepath.Join(runtime, "sshmcp-runner.sock"), "private Unix RPC socket")
	shellHome := set.String("home", home, "initial shell working directory and HOME")
	idle := set.Duration("idle-timeout", 15*time.Minute, "idle connection timeout")
	command := set.Duration("command-timeout", time.Hour, "maximum command duration")
	lifetime := set.Duration("max-lifetime", 24*time.Hour, "maximum connection lifetime")
	retention := set.Duration("result-ttl", 24*time.Hour, "terminal job retention and idempotency window")
	grace := set.Duration("cancel-grace", 10*time.Second, "SIGINT grace before killing the shell group")
	quota := set.Int64("output-limit", 8<<20, "maximum captured bytes per job stream")
	records := set.Int("max-records", 256, "maximum retained jobs and connections")
	if err := set.Parse(os.Args[2:]); err != nil {
		return err
	}
	if set.NArg() != 0 {
		return errors.New("unexpected positional arguments")
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	switch os.Args[1] {
	case "daemon":
		engine, err := runner.Open(runner.Config{
			Root:           *root,
			Socket:         *socket,
			Executable:     executable,
			Home:           *shellHome,
			IdleTimeout:    *idle,
			CommandTimeout: *command,
			MaxLifetime:    *lifetime,
			ResultTTL:      *retention,
			CancelGrace:    *grace,
			OutputLimit:    *quota,
			MaxRecords:     *records,
		})
		if err != nil {
			return err
		}
		serveErr := engine.Serve(ctx)
		return errors.Join(serveErr, engine.Close())
	case "shell-worker":
		return runner.RunWorker(ctx, *root, *socket, *shellHome, *grace, *quota)
	case "rpc":
		var request runner.Request
		decoder := json.NewDecoder(io.LimitReader(os.Stdin, runner.MaxRequestBytes))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&request); err != nil {
			return fmt.Errorf("request: %w", err)
		}
		rpcCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
		defer cancel()
		var response runner.Response
		if err := runner.Call(rpcCtx, *socket, request, &response); err != nil {
			return fmt.Errorf("RPC acknowledgement uncertain; use lookup by idempotency key: %w", err)
		}
		if err := json.NewEncoder(os.Stdout).Encode(response); err != nil {
			return err
		}
		if response.Error != "" {
			return errors.New(response.Code)
		}
		return nil
	default:
		return errors.New("unknown subcommand")
	}
}
