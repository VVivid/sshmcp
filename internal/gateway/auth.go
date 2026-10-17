// Package gateway exposes authenticated MCP tools over a private loopback HTTP
// listener. Durable execution belongs to the target runner, not HTTP sessions.
package gateway

import (
	"context"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/coreos/go-oidc/v3/oidc"
	"github.com/modelcontextprotocol/go-sdk/auth"

	"sshmcp/internal/runner"
)

const AdminScope = "sshmcp:admin"

// NewTokenVerifier discovers the pinned issuer and verifies signatures, issuer,
// resource audience, and expiration on every request. Never trust client owner
// fields or a transport session ID as an authenticated identity.
func NewTokenVerifier(ctx context.Context, issuer, resource string) (auth.TokenVerifier, error) {
	// The provider retains this client for later JWKS refreshes, whose background
	// context must still have an HTTP deadline after startup discovery finishes.
	ctx = oidc.ClientContext(ctx, &http.Client{Timeout: 8 * time.Second})
	provider, err := oidc.NewProvider(ctx, issuer)
	if err != nil {
		return nil, err
	}
	verifier := provider.Verifier(&oidc.Config{
		ClientID:             resource,
		SupportedSigningAlgs: []string{oidc.RS256},
	})
	return func(ctx context.Context, raw string, _ *http.Request) (*auth.TokenInfo, error) {
		token, err := verifier.Verify(ctx, raw)
		if err != nil || token.Subject == "" || token.Expiry.IsZero() {
			return nil, auth.ErrInvalidToken
		}
		var claims struct {
			Scope string `json:"scope"`
		}
		if token.Claims(&claims) != nil {
			return nil, auth.ErrInvalidToken
		}
		owner := runner.Owner{Issuer: token.Issuer, Subject: token.Subject}
		return &auth.TokenInfo{
			Scopes:     strings.Fields(claims.Scope),
			Expiration: token.Expiry,
			UserID:     token.Issuer + "\x00" + token.Subject,
			Extra:      map[string]any{"owner": owner},
		}, nil
	}, nil
}

func verifiedOwner(info *auth.TokenInfo) (runner.Owner, error) {
	if info != nil {
		if owner, ok := info.Extra["owner"].(runner.Owner); ok && owner.Issuer != "" && owner.Subject != "" {
			return owner, nil
		}
	}
	return runner.Owner{}, errors.New("verified identity is unavailable; authenticate again")
}
