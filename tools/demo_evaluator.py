"""Independent, read-only acceptance probes against real Grafana backends.

Every phase uses new W3C trace IDs and probe headers. A healthy HTTP response,
an actor's final message, or old telemetry can never satisfy these checks.
"""

import base64
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import lab
from demo_actor import save_json


LGTM_IMAGE = "grafana/otel-lgtm:0.37.0@sha256:659bd027d067bff346a0847dd6ab72433a8480404b35dedc24e43a5d565613b8"
APP_SERVICE = "hello-go"
PROXY_SERVICE = "traefik"
EXPORT_WAIT = 120


def environment(container):
    return dict(value.split("=", 1) for value in container["Config"].get("Env", []) if "=" in value)


def verify_architecture(containers):
    if set(containers) != {"app", "traefik", "lgtm"}:
        raise lab.LabError("Expected exactly the actor-created app, traefik, and lgtm Compose services.")
    for service, container in containers.items():
        if not container["State"]["Running"] or container["HostConfig"]["RestartPolicy"]["Name"] not in {"always", "unless-stopped"}:
            raise lab.LabError("Demo services must be running with persistent restart policies.")
        expected = {"traefik": {"8080"}, "lgtm": {"3000"}, "app": set()}[service]
        ports = set()
        for bindings in (container["HostConfig"].get("PortBindings") or {}).values():
            for binding in bindings or []:
                if binding.get("HostIp") != "127.0.0.1":
                    raise lab.LabError("A demo port is published beyond target loopback.")
                ports.add(binding["HostPort"])
        if ports != expected:
            raise lab.LabError("Demo published ports do not match target loopback 8080/3000.")
    if containers["lgtm"]["Config"]["Image"] != LGTM_IMAGE:
        raise lab.LabError("LGTM does not use the verified pinned ARM64 image.")
    env = environment(containers["app"])
    if not env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or any(re.fullmatch(r"OTEL_EXPORTER_OTLP_(TRACES|METRICS|LOGS)_ENDPOINT", key) for key in env):
        raise lab.LabError("App telemetry must use one generic OTLP endpoint without per-signal endpoint overrides.")


def identifier(value, size):
    if not value:
        return ""
    if re.fullmatch(r"[0-9a-fA-F]{%d}" % (size * 2), value):
        return value.lower()
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return ""
    return decoded.hex() if len(decoded) == size else ""


def trace_spans(document):
    """Tempo's OTLP JSON has resource batches, scope groups, then spans."""
    spans = []
    batches = document.get("batches", document.get("resourceSpans", []))
    for batch in batches:
        attributes = {item["key"]: item.get("value", {}).get("stringValue") for item in batch.get("resource", {}).get("attributes", [])}
        service = attributes.get("service.name")
        for scope in batch.get("scopeSpans", batch.get("instrumentationLibrarySpans", [])):
            for span in scope.get("spans", []):
                spans.append({"service": service, "trace_id": identifier(span.get("traceId"), 16),
                              "span_id": identifier(span.get("spanId"), 8), "parent_id": identifier(span.get("parentSpanId"), 8),
                              "started_ns": int(span.get("startTimeUnixNano", 0)), "kind": span.get("kind")})
    return spans


def connected_trace(document, trace_id, started_ns):
    spans = [span for span in trace_spans(document) if span["trace_id"] == trace_id and span["span_id"]]
    by_id = {span["span_id"]: span for span in spans}
    for app in spans:
        if app["service"] != APP_SERVICE or app["started_ns"] < started_ns - 5_000_000_000 or app["kind"] not in {2, "SPAN_KIND_SERVER"}:
            continue
        parent, visited = app["parent_id"], set()
        while parent and parent in by_id and parent not in visited:
            visited.add(parent)
            ancestor = by_id[parent]
            if ancestor["service"] == PROXY_SERVICE:
                return {"app_span_id": app["span_id"], "proxy_ancestor_id": parent, "trace_id": trace_id}
            parent = ancestor["parent_id"]
    return None


def loki_entries(document, started_ns, *, service=None):
    if document.get("status") != "success" or document.get("data", {}).get("resultType") != "streams":
        raise lab.LabError("Loki did not return a successful stream query.")
    return [value for stream in document["data"]["result"]
            if service is None or stream.get("stream", {}).get("service_name") == service
            for value in stream.get("values", []) if int(value[0]) >= started_ns - 5_000_000_000]


def proxy_json_log(entries, marker):
    for entry in entries:
        try:
            body = json.loads(entry[1])
        except (ValueError, TypeError):
            continue
        if isinstance(body, dict) and marker in json.dumps(body) and body.get("RequestMethod") == "GET" and int(body.get("DownstreamStatus", 0)) == 200:
            return True
    return False


def numeric_result(document):
    return document.get("status") == "success" and any(
        "value" in item and math.isfinite(float(item["value"][1]))
        for item in document.get("data", {}).get("result", [])
    )


def dashboard_expression(expression, textboxes):
    """Render the bounded five-minute dashboard context used by these probes."""
    values = {"__range": "5m", "__range_s": "300", "__range_ms": "300000",
              "__rate_interval": "5m", "__rate_interval_ms": "300000",
              "__interval": "1m", "__interval_ms": "60000"}

    def replace(match):
        name, format_name = match.group(1) or match.group(3), match.group(2)
        if name in values and format_name is None:
            return values[name]
        if name in textboxes and (format_name == "raw" or (format_name is None and textboxes[name] == "")):
            # Grafana's raw format deliberately preserves the scalar text,
            # including regular-expression syntax, without datasource escaping.
            return textboxes[name]
        raise lab.LabError(f"Dashboard query uses unsupported variable {match.group(0)}; only bounded time macros, declared empty textbox defaults, and declared scalar textbox raw values are supported.")

    variables = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?::([A-Za-z_][A-Za-z0-9_]*))?\}|([A-Za-z_][A-Za-z0-9_]*))")
    if "${" in variables.sub("", expression):
        raise lab.LabError("Dashboard query uses unsupported formatted variable syntax; inspect the saved dashboard query.")
    return variables.sub(replace, expression)


class Evaluator:
    def __init__(self, directory, *, username, password, wait=EXPORT_WAIT):
        self.directory = directory
        directory.mkdir(mode=0o700)
        self.wait = wait
        self.sequence = 0
        self.headers = {"Authorization": "Basic " + base64.b64encode((username + ":" + password).encode()).decode(), "Accept": "application/json"}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.sources = {}

    def json(self, path, params=None, *, missing_ok=False):
        self.sequence += 1
        url = "http://127.0.0.1:13000" + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        request = urllib.request.Request(url, headers=self.headers)
        status, raw = 0, b""
        try:
            with self.opener.open(request, timeout=10) as response:
                status = response.status
                raw = response.read(4 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read(4 * 1024 * 1024 + 1)
            error.close()
            if not (missing_ok and status == 404):
                raise lab.LabError(f"Independent Grafana/backend query failed (HTTP {status}).") from None
        finally:
            save_json(self.directory / f"query-{self.sequence:04d}.json", {"path": path, "params": params, "status": status,
                                                                         "body": raw.decode(errors="replace"), "observed_ns": time.time_ns()})
        if len(raw) > 4 * 1024 * 1024:
            raise lab.LabError("Independent backend response exceeded the finite evidence bound.")
        if missing_ok and status == 404:
            return None
        return json.loads(raw)

    def discover(self):
        values = self.json("/api/datasources")
        for kind in ("prometheus", "tempo", "loki"):
            matches = [source for source in values if source.get("type") == kind]
            if len(matches) != 1:
                raise lab.LabError("Expected one real datasource of each LGTM backend type.")
            self.sources[kind] = matches[0]
        save_json(self.directory / "datasources.json", values)

    def backend(self, kind, path, params=None, **options):
        uid = urllib.parse.quote(self.sources[kind]["uid"], safe="")
        return self.json("/api/datasources/proxy/uid/" + uid + path, params, **options)

    def traffic(self, *, count=1):
        probe = {"trace_id": uuid.uuid4().hex, "marker": "sshmcp-" + uuid.uuid4().hex, "started_ns": time.time_ns(), "count": count}
        for _ in range(count):
            headers = {"traceparent": "00-" + probe["trace_id"] + "-" + uuid.uuid4().hex[:16] + "-01", "X-SSHMCP-Probe": probe["marker"]}
            request = urllib.request.Request("http://127.0.0.1:18090/", headers=headers)
            with self.opener.open(request, timeout=10) as response:
                body = response.read(4097)
                if response.status != 200 or body.strip() != b"Hello, world!":
                    raise lab.LabError("Fresh request through the forwarded Traefik listener did not return Hello, world!.")
        save_json(self.directory / ("traffic-" + probe["marker"] + ".json"), probe)
        return probe

    def trace(self, probe):
        return self.backend("tempo", "/api/traces/" + probe["trace_id"], missing_ok=True)

    def logs(self, service, probe, *, trace=False):
        query = '{service_name=' + json.dumps(service) + "}"
        if trace:
            # OTLP trace_id is commonly Loki structured metadata. A backend
            # filter proves correlation even when the rendered line omits it.
            query += " | trace_id=" + json.dumps(probe["trace_id"])
        else:
            query += " |= " + json.dumps(probe["marker"])
        result = self.backend("loki", "/loki/api/v1/query_range", {"query": query, "start": probe["started_ns"] - 5_000_000_000,
                                                                    "end": time.time_ns(), "limit": 1000})
        return loki_entries(result, probe["started_ns"])

    def sample(self, selector, *, after_ns=None):
        if after_ns is not None:
            # An instant query's aggregate timestamp is the query time, not the
            # last exporter sample time. Filter the underlying series before
            # summing, so a pre-recreation counter cannot become a baseline.
            # timestamp() drops reserved type/unit labels. Ignore only those
            # metadata labels when matching each counter to its sample age.
            selector = "(" + selector + ") and ignoring(__type__,__unit__) (timestamp(" + selector + ") >= " + str(after_ns / 1_000_000_000) + ")"
        document = self.backend("prometheus", "/api/v1/query", {"query": "sum(" + selector + ")"})
        if document.get("status") != "success":
            raise lab.LabError("Prometheus rejected the independent counter query.")
        result = document.get("data", {}).get("result", [])
        if len(result) != 1 or "value" not in result[0]:
            return None
        value = float(result[0]["value"][1])
        return value if math.isfinite(value) else None

    def counters(self, *, fresh_since=None):
        # Prometheus 3.15's metadata endpoint only sees scrape targets, not
        # LGTM's OTLP ingestion. With this feature enabled, Prometheus derives
        # the reserved type label from OTLP's real type and monotonicity.
        flags = self.backend("prometheus", "/api/v1/status/flags")
        features = flags.get("data", {}).get("enable-feature", "").split(",")
        if flags.get("status") != "success" or "type-and-unit-labels" not in features:
            raise lab.LabError("Enable the bundled Prometheus type-and-unit-labels feature to prove received OTLP counter types.")
        document = self.backend("prometheus", "/api/v1/query", {"query": '{__name__=~".*[Rr]equest.*",__type__="counter"}'})
        if document.get("status") != "success":
            raise lab.LabError("Prometheus rejected independent typed-counter discovery.")
        vector = document.get("data", {}).get("result", [])
        counters = {}
        for service in (APP_SERVICE, PROXY_SERVICE):
            for result in sorted(vector, key=lambda item: item.get("metric", {}).get("__name__", "")):
                metric = result.get("metric", {})
                name = metric.get("__name__", "")
                if name.endswith("_count") or metric.get("__type__") != "counter":
                    continue
                # Request-byte volume is a counter too, but cannot prove how
                # many requests passed through the application or proxy.
                if "byte" in name.lower() or metric.get("__unit__", "").lower() in {"by", "byte", "bytes"}:
                    continue
                labels = {key: value for key, value in metric.items() if key in {"job", "service_name", "service"} and (value == service or value.endswith("/" + service))}
                if not labels:
                    continue
                labels["__type__"] = "counter"
                selector = name + "{" + ",".join(key + "=" + json.dumps(value) for key, value in sorted(labels.items())) + "}"
                baseline = self.sample(selector, after_ns=fresh_since) if fresh_since is not None else self.sample(selector)
                if baseline is not None:
                    counters[service] = {"name": name, "selector": selector, "before": baseline, "type_evidence": "prometheus-otlp-type-label"}
                    break
        return counters

    def dashboard(self, counters, app_logs, probe):
        dashboard_ids = self.json("/api/search", {"type": "dash-db", "limit": 100})
        if len(dashboard_ids) > 20:
            raise lab.LabError("Unexpected dashboard count exceeds the demo evaluation bound.")
        checks = {"requests": False, "latency": False, "app_logs": False, "proxy_logs": False, "trace_navigation": False}
        tempo_uid = self.sources["tempo"]["uid"]
        fields = self.sources["loki"].get("jsonData", {}).get("derivedFields", [])
        for field in fields:
            if field.get("datasourceUid") != tempo_uid or "__value.raw" not in field.get("url", ""):
                continue
            if field.get("matcherType") == "label" and field.get("matcherRegex", field.get("matcherName")) == "trace_id":
                # app_logs were already filtered by trace_id on the backend.
                checks["trace_navigation"] = bool(app_logs)
            elif field.get("matcherRegex"):
                try:
                    matches = [re.search(field["matcherRegex"], entry[1]) for entry in app_logs]
                except re.error:
                    raise lab.LabError("Grafana's log-to-trace matcher is invalid.") from None
                checks["trace_navigation"] |= any(match and probe["trace_id"] in match.groups() for match in matches)

        def panels(items):
            for panel in items:
                yield panel
                yield from panels(panel.get("panels", []))

        for item in dashboard_ids:
            uid = urllib.parse.quote(item["uid"], safe="")
            dashboard = self.json("/api/dashboards/uid/" + uid)["dashboard"]
            textboxes = {}
            for entry in dashboard.get("templating", {}).get("list", []):
                value = (entry.get("current") or {}).get("value")
                if entry.get("type") == "textbox" and isinstance(value, str) and len(value.encode()) <= 4096:
                    textboxes[entry.get("name")] = value
            for panel in panels(dashboard.get("panels", [])):
                for target in panel.get("targets", []):
                    source = target.get("datasource", panel.get("datasource", {}))
                    source_uid = source.get("uid") if isinstance(source, dict) else source
                    expression = target.get("expr", "")
                    if source_uid == self.sources["prometheus"]["uid"] and expression:
                        request_query = any(counter["name"] in expression for counter in counters.values())
                        latency_query = bool(re.search("duration|latency", expression, re.I))
                        if request_query or latency_query:
                            query = dashboard_expression(expression, textboxes)
                            response = self.backend("prometheus", "/api/v1/query", {"query": query})
                            checks["requests"] |= request_query and numeric_result(response)
                            checks["latency"] |= latency_query and numeric_result(response)
                    if source_uid == self.sources["loki"]["uid"] and expression and panel.get("type") == "logs":
                        query = dashboard_expression(expression, textboxes)
                        response = self.backend("loki", "/loki/api/v1/query_range", {"query": query, "start": probe["started_ns"] - 5_000_000_000,
                                                                                   "end": time.time_ns(), "limit": 100})
                        # A populated generic log panel is insufficient: its
                        # actual returned streams must include both services.
                        checks["app_logs"] |= bool(loki_entries(response, probe["started_ns"], service=APP_SERVICE))
                        checks["proxy_logs"] |= bool(loki_entries(response, probe["started_ns"], service=PROXY_SERVICE))
        save_json(self.directory / "dashboard-checks.json", checks)
        if not all(checks.values()):
            raise lab.LabError("Dashboard lacks working requests, latency, application/proxy logs, or trace navigation.")
        return checks

    def evaluate(self):
        self.discover()
        warm = self.traffic(count=3)
        deadline = time.monotonic() + self.wait
        counters = {}
        while time.monotonic() < deadline:
            counters = self.counters(fresh_since=warm["started_ns"])
            if len(counters) == 2:
                break
            time.sleep(2)
        if len(counters) != 2:
            raise lab.LabError("Fresh telemetry lacks genuine Go and Traefik request counters; histograms are not substituted.")
        probe = self.traffic(count=12)
        deadline = time.monotonic() + self.wait
        latest = {}
        while time.monotonic() < deadline:
            document = self.trace(probe)
            ancestry = connected_trace(document or {}, probe["trace_id"], probe["started_ns"])
            app_logs = self.logs(APP_SERVICE, probe, trace=True)
            proxy_logs = self.logs(PROXY_SERVICE, probe)
            for counter in counters.values():
                counter["after"] = self.sample(counter["selector"], after_ns=probe["started_ns"])
            increase = all(counter["after"] is not None and counter["after"] >= counter["before"] + probe["count"] for counter in counters.values())
            latest = {"probe": probe, "ancestry": ancestry, "app_log_count": len(app_logs), "proxy_json_log": proxy_json_log(proxy_logs, probe["marker"]),
                      "counters": counters, "counters_increased": increase}
            save_json(self.directory / "evaluation.json", latest)
            if ancestry and app_logs and latest["proxy_json_log"] and increase:
                latest["dashboard"] = self.dashboard(counters, app_logs, probe)
                save_json(self.directory / "evaluation.json", latest)
                return latest
            time.sleep(2)
        raise lab.LabError("Independent fresh trace/log/counter acceptance timed out; raw backend observations are retained.")

    def prove_fault(self, counters):
        self.discover()
        baseline = {service: {**counter, "before": self.sample(counter["selector"])} for service, counter in counters.items()}
        if any(counter["before"] is None for counter in baseline.values()):
            raise lab.LabError("Fault probe has no pre-traffic counter baseline.")
        probe = self.traffic(count=12)
        deadline = time.monotonic() + self.wait
        latest = {}
        while time.monotonic() < deadline:
            document = self.trace(probe) or {}
            app_spans = [span for span in trace_spans(document) if span["service"] == APP_SERVICE and span["trace_id"] == probe["trace_id"]]
            app_logs = self.logs(APP_SERVICE, probe, trace=True)
            proxy_logs = self.logs(PROXY_SERVICE, probe)
            latest = {"probe": probe, "app_spans": len(app_spans), "app_logs": len(app_logs), "proxy_json_log": proxy_json_log(proxy_logs, probe["marker"]),
                      "proxy_spans": sum(span["service"] == PROXY_SERVICE and span["trace_id"] == probe["trace_id"] for span in trace_spans(document)),
                      "app_counter": self.sample(baseline[APP_SERVICE]["selector"]), "app_counter_before": baseline[APP_SERVICE]["before"]}
            save_json(self.directory / "fault-evaluation.json", latest)
            if app_spans or app_logs:
                raise lab.LabError("Injected endpoint fault was ineffective: fresh Go telemetry still arrived.")
            time.sleep(2)
        if not latest.get("proxy_json_log") or not latest.get("proxy_spans") or latest.get("app_counter") != latest.get("app_counter_before"):
            raise lab.LabError("Fault did not independently preserve proxy/HTTP health while suppressing all fresh Go signals.")
        latest["observation_seconds"] = self.wait
        save_json(self.directory / "fault-evaluation.json", latest)
        return latest
