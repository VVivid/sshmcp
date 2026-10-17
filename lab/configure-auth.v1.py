#!/usr/bin/env python3
"""Narrow idempotent subject/refresh-scope setup for the owned private realm.

Runs on the control VM. Bootstrap-admin credentials stay there. Administration
also uses the normal login form and Authorization Code + S256 PKCE, never the
password grant. Existing realm, users, and their durable subject IDs are retained.
"""

import base64
from html.parser import HTMLParser
import hashlib
import http.cookiejar
import json
from pathlib import Path
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request


BASE = "http://127.0.0.1:18081"
ISSUER = BASE + "/realms/master"
CALLBACK = BASE + "/admin/master/console/"
RESOURCE = "http://127.0.0.1:18080/mcp"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Form(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = self.kind = None
        self.fields = {}

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "form" and attributes.get("id") in ("kc-form-login", "kc-update-profile-form"):
            self.action = attributes.get("action")
            self.kind = attributes["id"]
        if tag == "input" and attributes.get("type") == "hidden" and attributes.get("name"):
            self.fields[attributes["name"]] = attributes.get("value", "")


def admin_token():
    environment = dict(line.split("=", 1) for line in (Path.home() / ".sshmcp-control/.env").read_text().splitlines())
    password = environment["KEYCLOAK_ADMIN_PASSWORD"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    query = {"client_id": "security-admin-console", "redirect_uri": CALLBACK, "response_type": "code", "scope": "openid", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect())
    request = urllib.request.Request(ISSUER + "/protocol/openid-connect/auth?" + urllib.parse.urlencode(query))
    for _ in range(12):
        for cookie in jar:
            if cookie.domain == "127.0.0.1" and cookie.path == "/realms/master/":
                cookie.secure = False  # Match browsers' loopback secure context.
        try:
            with opener.open(request, timeout=10) as response:
                body = response.read(1 << 20).decode()
                current = response.url
        except urllib.error.HTTPError as error:
            if error.code not in (302, 303):
                error.close()
                raise RuntimeError("Private bootstrap authorization failed") from None
            location = urllib.parse.urljoin(request.full_url, error.headers["Location"])
            error.close()
            if location.startswith(CALLBACK + "?"):
                values = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
                if values.get("state") != [state] or values.get("iss") != [ISSUER] or "code" not in values:
                    raise RuntimeError("Private bootstrap OAuth callback rejected")
                fields = {"grant_type": "authorization_code", "client_id": "security-admin-console", "redirect_uri": CALLBACK, "code": values["code"][0], "code_verifier": verifier}
                request = urllib.request.Request(ISSUER + "/protocol/openid-connect/token", data=urllib.parse.urlencode(fields).encode())
                with urllib.request.urlopen(request, timeout=10) as response:
                    return json.load(response)["access_token"]
            if not location.startswith(ISSUER + "/"):
                raise RuntimeError("Unexpected bootstrap authorization redirect")
            request = urllib.request.Request(location)
            continue
        form = Form()
        form.feed(body)
        if not form.action:
            raise RuntimeError("Private bootstrap login form unavailable")
        action = urllib.parse.urljoin(current, form.action)
        if not action.startswith(ISSUER + "/login-actions/"):
            raise RuntimeError("Unexpected bootstrap form endpoint")
        if form.kind == "kc-form-login":
            fields = dict(form.fields, username="bootstrap-admin", password=password, credentialId="")
        else:
            fields = dict(form.fields, firstName="Bootstrap", lastName="Lab", email="bootstrap-admin@sshmcp.invalid")
        request = urllib.request.Request(action, data=urllib.parse.urlencode(fields).encode())
    raise RuntimeError("Private bootstrap OAuth redirect limit reached")


def main():
    deadline = time.monotonic() + 150
    while True:
        try:
            with urllib.request.urlopen(BASE + "/realms/sshmcp/.well-known/openid-configuration", timeout=5) as response:
                if response.status == 200:
                    break
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if time.monotonic() >= deadline:
                raise RuntimeError("Private authorization readiness timed out") from None
            time.sleep(1)
    token = admin_token()

    def api(path, data=None, method=None):
        request = urllib.request.Request(BASE + "/admin/realms/sshmcp/" + path,
            data=None if data is None else json.dumps(data).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method=method)
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read(1 << 20)
            return json.loads(body) if body else None

    clients = api("clients?clientId=sshmcp-codex")
    if len(clients) != 1 or clients[0]["clientId"] != "sshmcp-codex":
        raise RuntimeError("Private RP client identity is ambiguous")
    client = api("clients/" + clients[0]["id"])
    realm = api("")
    resources = api("clients?" + urllib.parse.urlencode({"clientId": RESOURCE}))
    if not resources:
        resources = api("clients?clientId=sshmcp-resource")
    if (
        realm.get("registrationAllowed")
        or not realm.get("enabled")
        or len(resources) != 1
        or resources[0].get("attributes", {}).get("resource_url") != RESOURCE
    ):
        raise RuntimeError("Private realm/resource configuration differs from its contract")
    if (
        not client.get("publicClient")
        or not client.get("standardFlowEnabled")
        or client.get("directAccessGrantsEnabled")
        or client.get("implicitFlowEnabled")
        or client.get("redirectUris") != ["http://127.0.0.1:18443/callback"]
        or client.get("attributes", {}).get("pkce.code.challenge.method") != "S256"
    ):
        raise RuntimeError("Private RP security configuration differs from its contract")
    audiences = [item for item in client.get("protocolMappers", []) if item.get("name") == "private-mcp-resource"]
    if (
        len(audiences) != 1
        or audiences[0].get("protocolMapper") != "oidc-audience-mapper"
        or audiences[0].get("config", {}).get("included.client.audience") not in ("sshmcp-resource", RESOURCE)
    ):
        raise RuntimeError("Private RP resource audience mapping differs from its contract")
    # Keep the resource's UUID and the RP's identity. The canonical URI as its
    # clientId keeps Included Client Audience stable on Keycloak refresh grants.
    resource = api("clients/" + resources[0]["id"])
    resource_id = resource["id"]
    if resource["clientId"] != RESOURCE:
        resource["clientId"] = RESOURCE
        api("clients/" + resource_id, resource, method="PUT")
    audience = audiences[0]
    if audience["config"]["included.client.audience"] != RESOURCE:
        audience["config"]["included.client.audience"] = RESOURCE
        api("clients/" + client["id"] + "/protocol-mappers/models/" + audience["id"], audience, method="PUT")
    if api("clients/" + resource_id)["id"] != resource_id or api("clients/" + client["id"])["clientId"] != "sshmcp-codex":
        raise RuntimeError("Private client identity changed during configuration")
    users_before = {user["username"]: user["id"] for user in api("users?max=100")}
    mapper = {"name": "verified-subject", "protocol": "openid-connect", "protocolMapper": "oidc-sub-mapper", "config": {"access.token.claim": "true", "introspection.token.claim": "true"}}
    matches = [item for item in client.get("protocolMappers", []) if item.get("name") == mapper["name"]]
    if matches:
        if len(matches) != 1 or any(matches[0].get(key) != value for key, value in mapper.items()):
            raise RuntimeError("Private subject mapper differs from its contract")
    else:
        api("clients/" + client["id"] + "/protocol-mappers/models", mapper)
    scopes = [scope for scope in api("client-scopes") if scope["name"] == "offline_access"]
    if not scopes:
        api("client-scopes", {"name": "offline_access", "protocol": "openid-connect", "attributes": {"include.in.token.scope": "true", "display.on.consent.screen": "true"}})
        scopes = [scope for scope in api("client-scopes") if scope["name"] == "offline_access"]
    if len(scopes) != 1:
        raise RuntimeError("Private offline refresh scope is ambiguous")
    optional = api("clients/" + client["id"] + "/optional-client-scopes")
    if not any(scope["id"] == scopes[0]["id"] for scope in optional):
        api("clients/" + client["id"] + "/optional-client-scopes/" + scopes[0]["id"], method="PUT")
    # Keycloak's standard offline_access scope includes this role mapping.
    # Minimal imported realms need both the scope and eligible owned users.
    roles = [role for role in api("roles") if role["name"] == "offline_access"]
    if not roles:
        api("roles", {"name": "offline_access"})
        roles = [role for role in api("roles") if role["name"] == "offline_access"]
    if len(roles) != 1:
        raise RuntimeError("Private offline refresh role is ambiguous")
    role = roles[0]
    mapping = "client-scopes/" + scopes[0]["id"] + "/scope-mappings/realm"
    if not any(item["id"] == role["id"] for item in api(mapping)):
        api(mapping, [role])
    for user_id in users_before.values():
        mapping = "users/" + user_id + "/role-mappings/realm"
        if not any(item["id"] == role["id"] for item in api(mapping + "/composite")):
            api(mapping, [role])
    users_after = {user["username"]: user["id"] for user in api("users?max=100")}
    if users_after != users_before or set(users_after) != {"admin", "other"}:
        raise RuntimeError("Private subject identities changed during mapper verification")
    print("Private subject mapper and optional refresh scope verified; existing user IDs retained.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # No traceback/body/URLs: any of those could contain an OAuth code/token.
        raise SystemExit("Private authorization configuration failed; inspect the owned services.") from None
