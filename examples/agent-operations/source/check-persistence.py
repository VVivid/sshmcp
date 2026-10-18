#!/usr/bin/env python3
"""Check a prior successful verification's data after recreation or reboot."""
import base64, json, pathlib, urllib.parse, urllib.request
root = pathlib.Path(__file__).resolve().parent
before = json.loads((root / "verification-before-reboot.json").read_text())
password = dict(line.split("=", 1) for line in (root / ".env").read_text().splitlines() if "=" in line)["GF_SECURITY_ADMIN_PASSWORD"]
auth = "Basic " + base64.b64encode(("admin:" + password).encode()).decode()
def get(path):
    with urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:3000" + path, headers={"Authorization": auth}), timeout=20) as response:
        return json.load(response)
boot_id = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
assert boot_id != before["boot_id"], "VM has not rebooted since the saved verification"
trace = get("/api/datasources/proxy/uid/tempo/api/traces/" + before["trace_id"])
assert trace.get("batches", trace.get("resourceSpans")), "Prior trace missing"
for service in ["hello-go", "traefik"]:
    query = '{service_name="' + service + '"} |= "' + before["trace_id"] + '"'
    data = get("/api/datasources/proxy/uid/loki/loki/api/v1/query_range?" + urllib.parse.urlencode({"query": query, "limit": 10, "since": "24h"}))
    assert data["data"]["result"], service + " prior log missing"
expr = 'sum(hello_requests_total{__type__="counter"} @ ' + str(int(__import__("datetime").datetime.fromisoformat(before["checked_at"]).timestamp()) + 10) + ')'
data = get("/api/datasources/proxy/uid/prometheus/api/v1/query?" + urllib.parse.urlencode({"query": expr}))
assert data["data"]["result"], "Prior metric sample missing"
assert get("/api/dashboards/uid/sshmcp-hello")["dashboard"]["uid"] == "sshmcp-hello"
result = {"previous_boot_id": before["boot_id"], "current_boot_id": boot_id, "prior_trace_id": before["trace_id"], "persistent_signals": ["metrics", "application logs", "proxy logs", "traces", "Grafana dashboard"], "status": "passed"}
(root / "verification-persistence.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
