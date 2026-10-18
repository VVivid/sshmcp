#!/usr/bin/env python3
"""Exercise requests and verify the deployment through Grafana's datasource APIs."""
import base64, datetime, json, math, os, pathlib, re, secrets, subprocess, time
import urllib.parse, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
password = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines() if "=" in line)["GF_SECURITY_ADMIN_PASSWORD"]
auth = "Basic " + base64.b64encode(("admin:" + password).encode()).decode()
def get(path):
    request = urllib.request.Request("http://127.0.0.1:3000" + path, headers={"Authorization": auth})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)
def prom(expr):
    data = get("/api/datasources/proxy/uid/prometheus/api/v1/query?" + urllib.parse.urlencode({"query": expr}))
    assert data["status"] == "success", data
    return data["data"]["result"]
def counter(name):
    result = prom('sum(' + name + '{__type__="counter"})')
    return float(result[0]["value"][1]) if result else 0
def logs(service, probe):
    data = get("/api/datasources/proxy/uid/loki/loki/api/v1/query_range?" + urllib.parse.urlencode({"query": '{service_name="' + service + '"} |= "' + probe + '"', "limit": 100}))
    return data["data"]["result"]
def eventually(fn, seconds=60):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return fn()
        except Exception as exc:
            print("Waiting for verification:", type(exc).__name__, str(exc)[:250], flush=True)
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)

def ready():
    assert get("/api/health")["database"] == "ok"
    with urllib.request.urlopen("http://127.0.0.1:8080/", timeout=3) as response:
        assert response.read() == b"Hello, world!\n"
eventually(ready, 120)
# Wait for the readiness request to be reflected after a process restart.
time.sleep(8)
before = {name: counter(name) for name in ["hello_requests_total", "traefik_entrypoint_requests_total"]}
trace_id, parent_id = secrets.token_hex(16), secrets.token_hex(8)
probe = "verify-" + secrets.token_hex(6)
for i in range(31):
    request = urllib.request.Request("http://127.0.0.1:8080/", headers={"X-SSHMCP-Probe": probe if i == 0 else probe + "-load", **({"traceparent": "00-" + trace_id + "-" + parent_id + "-01"} if i == 0 else {})})
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
        assert response.read() == b"Hello, world!\n"
        if i == 0:
            assert response.headers["X-Trace-ID"] == trace_id
    time.sleep(0.35)
report = {"checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "trace_id": trace_id, "upstream_parent_id": parent_id, "probe": probe, "requests_sent": 31}
def check():
    after = {name: counter(name) for name in before}
    assert all(after[name] >= before[name] + 31 for name in before), (before, after)
    found_logs = {}
    for service in ["hello-go", "traefik"]:
        candidates = [s for s in logs(service, probe) if s["stream"].get("trace_id") == trace_id]
        assert candidates, service + " correlated OTLP log missing"
        found_logs[service] = candidates[0]
    trace = get("/api/datasources/proxy/uid/tempo/api/traces/" + trace_id)
    spans = []
    for batch in trace.get("batches", trace.get("resourceSpans", [])):
        attrs = {a["key"]: a["value"].get("stringValue") for a in batch["resource"]["attributes"]}
        for scope in batch.get("scopeSpans", []):
            for span in scope["spans"]:
                spans.append({"service": attrs["service.name"], "name": span["name"], "kind": span["kind"], "id": base64.b64decode(span["spanId"]).hex(), "parent": base64.b64decode(span.get("parentSpanId", "")).hex()})
    app = next(s for s in spans if s["service"] == "hello-go" and s["kind"] == "SPAN_KIND_SERVER")
    proxy_client = next(s for s in spans if s["service"] == "traefik" and s["id"] == app["parent"])
    proxy_server = next(s for s in spans if s["service"] == "traefik" and s["id"] == proxy_client["parent"])
    assert proxy_server["parent"] == parent_id
    assert found_logs["hello-go"]["stream"]["span_id"] == app["id"]
    proxy_body = json.loads(found_logs["traefik"]["values"][0][1])
    assert proxy_body["request_X-Sshmcp-Probe"] == probe
    assert proxy_body["TraceId"] == trace_id
    source = get("/api/datasources/uid/loki")
    field = source["jsonData"]["derivedFields"][0]
    assert field["datasourceUid"] == "tempo" and field["url"] == "${__value.raw}"
    for service in found_logs:
        assert re.search(field["matcherRegex"], found_logs[service]["values"][0][1]).group(1) == trace_id
    dashboard = get("/api/dashboards/uid/sshmcp-hello")["dashboard"]
    panel_results = {}
    for panel in dashboard["panels"]:
        if panel["type"] in ["timeseries", "stat"]:
            expr = panel["targets"][0]["expr"].replace("$__rate_interval", "2m").replace("$__range", "15m")
            result = prom(expr)
            assert result and all(math.isfinite(float(r["value"][1])) for r in result), (panel["title"], result)
            panel_results[panel["title"]] = [r["value"][1] for r in result]
    return {"counters_before": before, "counters_after": after, "spans": spans, "log_services": list(found_logs), "dashboard_panels": panel_results, "log_to_trace_link": "validated for both log bodies and Tempo datasource"}
report.update(eventually(check))
cfg = json.loads(subprocess.check_output(["sudo", "-n", "docker", "compose", "-f", str(ROOT / "compose.yaml"), "config", "--format", "json"], cwd=ROOT))
assert cfg["name"] == "sshmcp-demo" and set(cfg["services"]) == {"app", "traefik", "lgtm"}
app_env = cfg["services"]["app"]["environment"]
assert [k for k in app_env if k.startswith("OTEL_EXPORTER_OTLP") and "ENDPOINT" in k] == ["OTEL_EXPORTER_OTLP_ENDPOINT"]
assert len(password) >= 16 and (ROOT / ".env").stat().st_mode & 0o077 == 0
for path in ROOT.rglob("*"):
    if path.is_file() and path.name != ".env":
        assert password.encode() not in path.read_bytes(), str(path)
for name, service in cfg["services"].items():
    assert service["restart"] == "unless-stopped"
    expected = {"app": [], "traefik": [(8080, 80)], "lgtm": [(3000, 3000)]}[name]
    actual = [(int(p["published"]), int(p["target"])) for p in service.get("ports", [])]
    assert actual == expected, (name, actual)
    assert all(p["host_ip"] == "127.0.0.1" for p in service.get("ports", []))
report["configuration"] = "three services; loopback ports; one generic app endpoint; restart policies; private credential file"
report["boot_id"] = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
(ROOT / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
