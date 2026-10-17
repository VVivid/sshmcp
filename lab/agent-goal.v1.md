# Private deployment goal, version 1

Use the private SSHMCP tools to discover the target operating system and build a
working, observable demo from scratch. Install Docker Engine and Docker Compose,
write a Go HTTP application returning `Hello, world!`, and deploy it behind
Traefik. Instrument the Go application's request logs, metrics, and traces with
OpenTelemetry. Deploy Grafana LGTM and use its bundled OpenTelemetry Collector
for the application's telemetry, Traefik's OTLP metrics/traces, and Traefik's
JSON access logs. Create a useful Grafana dashboard showing request counts/rate,
latency, application/proxy logs, and navigation from a correlated log to its
trace. Leave the deployment running and usable after a VM stop/start.

Component constraints:

- Use one Docker Compose project named `sshmcp-demo` and exactly three services:
  `app`, `traefik`, and `lgtm`. Set OpenTelemetry `service.name` to `hello-go` for
  Go and `traefik` for the proxy, including collected proxy logs. Use the bundled
  LGTM collector rather than adding a fourth collector container.
- Use the verified ARM64 LGTM image
  `grafana/otel-lgtm:0.37.0@sha256:659bd027d067bff346a0847dd6ab72433a8480404b35dedc24e43a5d565613b8`.
  Its official documentation is at
  <https://grafana.com/docs/opentelemetry/docker-lgtm/>. Discover any configuration
  details you need through the target; no source or deployment recipe is supplied.
- Enable the bundled Prometheus `type-and-unit-labels` feature so received OTLP
  metric types remain independently queryable. Let Prometheus derive its
  reserved type labels from the incoming telemetry.
- Publish only Traefik HTTP on target `127.0.0.1:8080` and Grafana on target
  `127.0.0.1:3000`. Do not publish the application or telemetry backend ports.
  The application must serve `/` through Traefik without a special Host header.
- Configure the application with one generic `OTEL_EXPORTER_OTLP_ENDPOINT`
  environment variable. All three Go signals must honor it. Do not set
  per-signal endpoint overrides or hardcode exporter endpoints in source.
  Use a genuine monotonic application request counter, as well as latency data;
  native histograms alone do not replace a request counter. Enable Traefik's
  request counters and OTLP tracing with full sampling for this small demo.
- Preserve incoming W3C trace context, create a Go HTTP server span beneath
  Traefik's spans, and emit request logs through OTLP with that active trace
  context. Preserve the `X-SSHMCP-Probe` request header in Traefik's JSON access
  logs so an operator can correlate a particular request independently.
- Choose your own deployment directory and files beneath the test user's home.
  Keep all source/config in that single directory, separate from binary build
  output and Docker named volumes used for telemetry and log data. Do not add
  Mac home/repository mounts into the Ubuntu VM. Scoped read-only Docker bind
  mounts of your Ubuntu source/config files are allowed. Keep credentials in private `.env` files, referenced by
  configuration; use a generated Grafana admin password of at least 16 characters
  through `GF_SECURITY_ADMIN_PASSWORD`. Do not print credentials in your final
  response. Other source/config files must contain no literal credentials.
- Use restart policies and persistent volumes so the demo resumes from its disks.
  Keep the existing SSH, native runner, account, systemd, and lab trust intact.

Choose and create the application, instrumentation, collector configuration,
Docker build, proxy configuration, and dashboard yourself. Use the installed
agent's normal account and model. When finished, report what you deployed,
checks actually performed, and any remaining limitations or tool friction.
