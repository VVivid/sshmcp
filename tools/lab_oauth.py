"""Owned Keycloak lab-account Authorization Code + S256 PKCE helpers.

Passwords and tokens stay in private files/memory. This exercises the normal
login form; it never uses the password grant or impersonates another service.
"""

import base64
from html.parser import HTMLParser
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import select
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import mcp_lab


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LoginForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = None
        self.kind = None
        self.fields = {}

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if tag == "form" and values.get("id") in ("kc-form-login", "kc-update-profile-form"):
            self.action = values.get("action")
            self.kind = values["id"]
        if tag == "input" and values.get("name") and values.get("type") == "hidden":
            self.fields[values["name"]] = values.get("value", "")


def login_callback(authorization_url, account="admin"):
    """Return the exact callback URL without following it or printing secrets."""
    parsed = urllib.parse.urlparse(authorization_url)
    if parsed.scheme != "http" or parsed.netloc != "127.0.0.1:18081" or not parsed.path.startswith("/realms/sshmcp/"):
        raise RuntimeError("Refusing credentials for an unexpected authorization endpoint")
    original = urllib.parse.parse_qs(parsed.query)
    if original.get("redirect_uri") != [mcp_lab.CALLBACK]:
        raise RuntimeError("Unexpected OAuth callback")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect())
    request = urllib.request.Request(authorization_url)
    for _ in range(12):
        # Browsers treat loopback as a secure context. CookieJar does not, so
        # mirror that exception only for this exact private loopback realm.
        for cookie in jar:
            if cookie.domain == "127.0.0.1" and cookie.path == "/realms/sshmcp/":
                cookie.secure = False
        try:
            with opener.open(request, timeout=10) as response:
                body = response.read(1 << 20).decode()
                current_url = response.url
        except urllib.error.HTTPError as error:
            if error.code not in (301, 302, 303, 307, 308):
                mcp_lab.lab.write_private(mcp_lab.PRIVATE / "oauth-error.html", error.read(1 << 20).decode(errors="replace"))
                error.close()
                raise RuntimeError(f"Authorization endpoint returned HTTP {error.code}") from None
            location = urllib.parse.urljoin(request.full_url, error.headers["Location"])
            error.close()
            redirect = urllib.parse.urlparse(location)
            if redirect.scheme == "http" and redirect.netloc == "127.0.0.1:18443" and redirect.path == "/callback":
                values = urllib.parse.parse_qs(redirect.query)
                if values.get("state") != original.get("state") or values.get("iss") != [mcp_lab.ISSUER]:
                    raise RuntimeError("OAuth callback state or issuer mismatch")
                return location
            if redirect.scheme != "http" or redirect.netloc != "127.0.0.1:18081":
                raise RuntimeError("Unexpected authorization redirect")
            request = urllib.request.Request(location)
            continue
        form = LoginForm()
        form.feed(body)
        if not form.action:
            mcp_lab.lab.write_private(mcp_lab.PRIVATE / "oauth-error.html", body)
            raise RuntimeError("Keycloak did not provide its normal login form")
        action = urllib.parse.urljoin(current_url, form.action)
        if not action.startswith(mcp_lab.ISSUER + "/login-actions/"):
            raise RuntimeError("Unexpected credential submission endpoint")
        if form.kind == "kc-form-login":
            fields = dict(form.fields, username=account, password=mcp_lab.accounts()[account], credentialId="")
        else:
            fields = dict(form.fields, firstName=account, lastName="Lab", email=account + "@sshmcp.invalid")
        request = urllib.request.Request(action, data=urllib.parse.urlencode(fields).encode())
    raise RuntimeError("OAuth redirect limit reached")


def authorization_code(account="admin", resource=mcp_lab.RESOURCE, scope="sshmcp:admin"):
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    query = {"client_id": mcp_lab.CLIENT, "redirect_uri": mcp_lab.CALLBACK, "response_type": "code",
             "scope": scope, "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "resource": resource}
    callback = login_callback(mcp_lab.ISSUER + "/protocol/openid-connect/auth?" + urllib.parse.urlencode(query), account)
    parameters = urllib.parse.parse_qs(urllib.parse.urlparse(callback).query)
    if "error" in parameters:
        return parameters, verifier
    if "code" not in parameters:
        raise RuntimeError("Missing authorization code")
    return parameters, verifier


def token_request(fields, *, timeout=10):
    fields = dict(fields, client_id=mcp_lab.CLIENT)
    request = urllib.request.Request(mcp_lab.ISSUER + "/protocol/openid-connect/token", data=urllib.parse.urlencode(fields, doseq=True).encode())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        with error:
            return json.loads(error.read(1 << 16))


def tokens(account="admin", scope="sshmcp:admin"):
    parameters, verifier = authorization_code(account, scope=scope)
    if "error" in parameters:
        raise RuntimeError("Authorization code rejected: " + parameters["error"][0])
    result = token_request({"grant_type": "authorization_code", "code": parameters["code"][0],
                            "redirect_uri": mcp_lab.CALLBACK, "code_verifier": verifier, "resource": mcp_lab.RESOURCE})
    if "access_token" not in result:
        raise RuntimeError("Token exchange failed: " + result.get("error", "unknown"))
    return result


def codex_login(directory):
    """Complete the installed CLI's real one-time flow with an owned account."""
    command = mcp_lab.codex_arguments("mcp", "login", mcp_lab.SERVER_NAME, "--no-browser", "--scopes", "sshmcp:admin,offline_access")
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=mcp_lab.lab.ROOT)
    transcript = bytearray()
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], 1)
            if ready:
                data = os.read(process.stdout.fileno(), 8192)
                transcript.extend(data)
                if not data:
                    break
                match = re.search(r"http://127\.0\.0\.1:18081/[^\s<>\"]+", transcript.decode(errors="replace"))
                if match:
                    callback = login_callback(match.group())
                    output, _ = process.communicate((callback + "\n").encode(), timeout=60)
                    transcript.extend(output)
                    if process.returncode:
                        raise RuntimeError("Codex OAuth login failed; inspect private transcript")
                    return
        raise RuntimeError("Codex did not provide its authorization URL before the deadline")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate(timeout=5)
            transcript.extend(output)
        # Preserve each attempt, including interoperability failures, privately.
        stamp = str(time.time_ns())
        mcp_lab.lab.write_private(directory / ("codex-login-" + stamp + ".log"), transcript.decode(errors="replace"))
