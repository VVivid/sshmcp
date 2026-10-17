package gateway

import (
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"time"
)

// NewAuthorizationProxy fronts only the owned Keycloak loopback listener. The
// pinned experimental resource-indicators implementation ignores resource on
// refresh grants. Enforce the single private resource before any token exchange
// reaches it; signature, account login, PKCE, grants and token minting remain
// Keycloak's responsibility. The upstream is never configurable or forwarded.
func NewAuthorizationProxy(resource string) http.Handler {
	proxy := httputil.NewSingleHostReverseProxy(&url.URL{Scheme: "http", Host: "127.0.0.1:18082"})
	proxy.Transport = &http.Transport{
		DialContext:           (&net.Dialer{Timeout: 5 * time.Second}).DialContext,
		MaxIdleConns:          32,
		MaxIdleConnsPerHost:   16,
		MaxConnsPerHost:       32,
		IdleConnTimeout:       60 * time.Second,
		ResponseHeaderTimeout: 20 * time.Second,
	}
	proxy.ErrorHandler = func(w http.ResponseWriter, _ *http.Request, _ error) {
		authorizationError(w, "temporarily_unavailable", "Private authorization service is unavailable", http.StatusBadGateway)
	}
	guard := guardResource(proxy, resource)
	slots := make(chan struct{}, 32)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Host != "127.0.0.1:18081" {
			http.Error(w, "unexpected Host", http.StatusBadRequest)
			return
		}
		select {
		case slots <- struct{}{}:
			defer func() { <-slots }()
		default:
			authorizationError(w, "temporarily_unavailable", "Authorization capacity reached", http.StatusTooManyRequests)
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
		guard.ServeHTTP(w, r)
	})
}

func authorizationError(w http.ResponseWriter, code, description string, status int) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(map[string]string{"error": code, "error_description": description})
}

func guardResource(next http.Handler, resource string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path := "/realms/sshmcp/protocol/openid-connect/"
		var values url.Values
		var err error
		requireResource := false
		switch {
		case r.URL.Path == path+"auth":
			values, err = url.ParseQuery(r.URL.RawQuery)
			requireResource = values.Get("client_id") == "sshmcp-codex"
		case r.URL.Path == path+"token" && r.Method == http.MethodPost:
			if len(r.URL.Query()["resource"]) > 0 {
				authorizationError(w, "invalid_request", "Token resource must be supplied once in the form body", http.StatusBadRequest)
				return
			}
			r.Body = http.MaxBytesReader(w, r.Body, 64<<10)
			err = r.ParseForm()
			values = r.Form
			if err == nil {
				body := r.PostForm.Encode()
				r.Body = io.NopCloser(strings.NewReader(body))
				r.ContentLength = int64(len(body))
			}
			requireResource = values.Get("grant_type") == "authorization_code" && values.Get("client_id") == "sshmcp-codex"
		default:
			next.ServeHTTP(w, r)
			return
		}
		if err != nil {
			authorizationError(w, "invalid_request", "Malformed or oversized authorization parameters", http.StatusBadRequest)
			return
		}
		resources := values["resource"]
		if len(resources) > 1 {
			authorizationError(w, "invalid_request", "Exactly one resource parameter is allowed", http.StatusBadRequest)
			return
		}
		if len(resources) == 1 && resources[0] != resource ||
			requireResource && len(resources) != 1 {
			authorizationError(w, "invalid_target", "The requested resource is not the private MCP endpoint", http.StatusBadRequest)
			return
		}
		// An omitted refresh resource keeps the grant's original audience. Using
		// the canonical resource URI as Keycloak's resource client ID makes its
		// Included Client Audience stable even on this experimental refresh path.
		next.ServeHTTP(w, r)
	})
}
