package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"sshmcp/internal/gateway"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	path := flag.String("config", "", "private gateway JSON configuration")
	flag.Parse()
	if *path == "" || flag.NArg() != 0 {
		return errors.New("usage: sshmcp-gateway --config /private/config.json")
	}
	file, err := os.Open(*path)
	if err != nil {
		return err
	}
	defer file.Close()
	var config gateway.Config
	decoder := json.NewDecoder(file)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&config); err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	// Public issuer URL stays on 18081. Keycloak itself binds only the separate
	// unforwarded loopback upstream, so all OAuth clients cross the resource guard.
	authListener, err := net.Listen("tcp", "127.0.0.1:18081")
	if err != nil {
		return err
	}
	authServer := &http.Server{
		Handler:           gateway.NewAuthorizationProxy(config.Resource),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      40 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    16 << 10,
	}
	defer authServer.Close()
	authFailure := make(chan error, 1)
	go func() {
		if err := authServer.Serve(authListener); err != nil && !errors.Is(err, http.ErrServerClosed) {
			authFailure <- err
			stop()
		}
	}()
	discoveryCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	verifier, err := gateway.NewTokenVerifier(discoveryCtx, config.Issuer, config.Resource)
	cancel()
	if err != nil {
		return fmt.Errorf("issuer discovery unavailable: %w", err)
	}
	rpc, err := gateway.NewSSHRunner(config.SSH)
	if err != nil {
		return err
	}
	defer rpc.Close()
	handler, err := gateway.NewHandler(config, rpc, verifier)
	if err != nil {
		return err
	}
	server := &http.Server{
		Addr:              config.Listen,
		Handler:           handler,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      40 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    16 << 10,
	}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		server.Shutdown(shutdownCtx)
		authServer.Shutdown(shutdownCtx)
	}()
	err = server.ListenAndServe()
	if errors.Is(err, http.ErrServerClosed) {
		select {
		case authErr := <-authFailure:
			return fmt.Errorf("private authorization listener: %w", authErr)
		default:
		}
		return nil
	}
	return err
}
