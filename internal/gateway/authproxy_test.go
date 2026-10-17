package gateway

import (
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

func TestResourceGuard(t *testing.T) {
	const resource = "http://127.0.0.1:18080/mcp"
	for _, tc := range []struct {
		name, grant, values string
		want                int
	}{
		{"code-intended", "authorization_code", "resource=" + url.QueryEscape(resource), 204},
		{"code-missing", "authorization_code", "", 400},
		{"refresh-intended", "refresh_token", "resource=" + url.QueryEscape(resource), 204},
		{"refresh-omitted", "refresh_token", "", 204},
		{"refresh-unknown", "refresh_token", "resource=http%3A%2F%2Funknown.invalid", 400},
		{"duplicate", "refresh_token", "resource=" + url.QueryEscape(resource) + "&resource=" + url.QueryEscape(resource), 400},
		{"conflict", "refresh_token", "resource=" + url.QueryEscape(resource) + "&resource=other", 400},
	} {
		t.Run(tc.name, func(t *testing.T) {
			called := false
			handler := guardResource(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				called = true
				if r.ParseForm() != nil || r.Form.Get("grant_type") != tc.grant {
					t.Error("form was not preserved")
				}
				w.WriteHeader(204)
			}), resource)
			body := "client_id=sshmcp-codex&grant_type=" + tc.grant + "&" + tc.values
			request := httptest.NewRequest("POST", "http://127.0.0.1:18081/realms/sshmcp/protocol/openid-connect/token", strings.NewReader(body))
			request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, request)
			if response.Code != tc.want || called != (tc.want == 204) {
				t.Fatalf("HTTP %d, called %v: %s", response.Code, called, response.Body.String())
			}
		})
	}
}
