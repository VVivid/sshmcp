# SSHMCP Hello Go demo

This Ubuntu ARM64 deployment uses one Compose project, sshmcp-demo, with three
services: app, traefik, and lgtm. Docker Engine starts at boot. All three containers
have unless-stopped restart policies. Avoid manually stopping containers before a
VM stop/start if you want Docker to bring them back automatically.

## Use

On the target, GET http://127.0.0.1:8080/ returns Hello, world! through Traefik.
Grafana is http://127.0.0.1:3000/d/sshmcp-hello. Sign in as admin with the generated
password in the private .env file. Anonymous access and self registration are
disabled. No telemetry ports or application port are published.

For remote browser access, forward local ports 3000 and 8080 to the target's
127.0.0.1 ports with your existing SSH access.

The home dashboard contains application/proxy counters, rate and p95 latency
charts, and both request log streams. Use the probe text box to search a unique
X-SSHMCP-Probe header value. Expand a log line and select View trace. Tempo shows
Traefik's server/client spans and the Go server span, including the incoming W3C
parent when supplied. Trace-to-log links are also provisioned.

Run ./demo-traffic.sh 60 for a minute of traffic, or:
curl -H 'X-SSHMCP-Probe: my-investigation' http://127.0.0.1:8080/

## Operations

Run ./deploy.sh to rebuild and deploy. Run python3 verify.py to exercise requests,
check all signals, validate span ancestry/log links and query all metric panels.
The last successful report is verification.json.

Use sudo docker compose ps and sudo docker compose logs --tail=100 in this
directory for status. No Docker socket is mounted in a service. The Go binary is
built inside Docker and runs as a non-root user in a read-only filesystem.

Source/configuration stays here. Binary layers and build cache stay in Docker.
Docker named volumes sshmcp-demo_lgtm-data and sshmcp-demo_proxy-logs hold backend
data, collector queues/cursors and JSON proxy logs separately. Normal compose
recreation preserves them. Do not use docker compose down -v unless deleting data
is intentional. Back up the source directory, private .env and both named volumes.

## Telemetry details

Go uses OTLP HTTP protobuf exporters for logs, metrics and traces. All exporters
read only the generic OTEL_EXPORTER_OTLP_ENDPOINT setting; no per-signal endpoint
overrides or source endpoints are used. The application emits a monotonic
hello.requests counter, a duration histogram, standard HTTP server metrics, and
request log records with the active span context. Health checks bypass telemetry.

Traefik emits its request counters/histograms and fully sampled OTLP traces to the
bundled collector. Its JSON access log preserves X-SSHMCP-Probe. The same
collector tails this named volume, parses timestamps and trace/span IDs, and sets
the proxy log resource service.name to traefik. Application service.name is
hello-go.

The pinned LGTM image's bundled collector sends metrics to bundled Prometheus,
logs to Loki, and traces to Tempo. Prometheus type-and-unit-labels is enabled.
Prometheus derives __type__ and __unit__ from OTLP; configuration never injects
those reserved labels. Queries explicitly select counter or histogram series.

## Scope and limitations

This is a local development/demo stack, as intended by the LGTM image. There is
no HA or TLS. Metrics have seven-day retention; Loki/Tempo use the image's bundled
defaults. Collector export queues and file offsets persist, but application SDK
buffers are in memory and abrupt power loss can lose in-flight telemetry.
Proxy access.json is persistent and is not automatically rotated; monitor its
volume usage for long-running/high-traffic use. Rates need multiple metric
exports and latency quantiles need recent traffic. Counts reset when processes
restart; rate/increase queries handle counter resets.
