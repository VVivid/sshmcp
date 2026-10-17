package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploghttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	otellog "go.opentelemetry.io/otel/log"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/propagation"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

func main() {
	if len(os.Args) > 1 && os.Args[1] == "-health" {
		c := http.Client{Timeout: 2 * time.Second}
		r, e := c.Get("http://127.0.0.1:8080/healthz")
		if e != nil {
			os.Exit(1)
		}
		r.Body.Close()
		if r.StatusCode != 200 {
			os.Exit(1)
		}
		return
	}
	ctx := context.Background()
	res, e := resource.New(ctx, resource.WithFromEnv(), resource.WithTelemetrySDK(), resource.WithAttributes(attribute.String("service.name", "hello-go")))
	if e != nil {
		log.Fatal(e)
	}
	te, e := otlptracehttp.New(ctx)
	if e != nil {
		log.Fatal(e)
	}
	tp := sdktrace.NewTracerProvider(sdktrace.WithResource(res), sdktrace.WithSampler(sdktrace.ParentBased(sdktrace.AlwaysSample())), sdktrace.WithBatcher(te))
	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	me, e := otlpmetrichttp.New(ctx)
	if e != nil {
		log.Fatal(e)
	}
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithResource(res), sdkmetric.WithReader(sdkmetric.NewPeriodicReader(me, sdkmetric.WithInterval(5*time.Second))))
	otel.SetMeterProvider(mp)
	le, e := otlploghttp.New(ctx)
	if e != nil {
		log.Fatal(e)
	}
	lp := sdklog.NewLoggerProvider(sdklog.WithResource(res), sdklog.WithProcessor(sdklog.NewBatchProcessor(le, sdklog.WithExportInterval(time.Second))))
	logger := lp.Logger("hello-go/requests")
	meter := mp.Meter("hello-go/requests")
	count, e := meter.Int64Counter("hello.requests", metric.WithDescription("Completed application HTTP requests"), metric.WithUnit("{request}"))
	if e != nil {
		log.Fatal(e)
	}
	duration, e := meter.Float64Histogram("hello.request.duration", metric.WithUnit("s"), metric.WithDescription("Application request duration"), metric.WithExplicitBucketBoundaries(.001, .005, .01, .025, .05, .1, .25, .5, 1, 2))
	if e != nil {
		log.Fatal(e)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) })
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sc := trace.SpanContextFromContext(r.Context())
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.Header().Set("X-Trace-ID", sc.TraceID().String())
		fmt.Fprintln(w, "Hello, world!")
		elapsed := time.Since(start).Seconds()
		attrs := []attribute.KeyValue{attribute.String("http.request.method", r.Method), attribute.String("http.route", "/"), attribute.Int("http.response.status_code", 200)}
		count.Add(r.Context(), 1, metric.WithAttributes(attrs...))
		duration.Record(r.Context(), elapsed, metric.WithAttributes(attrs...))
		body, _ := json.Marshal(map[string]any{"message": "request completed", "method": r.Method, "path": r.URL.Path, "status": 200, "duration_seconds": elapsed, "probe": r.Header.Get("X-SSHMCP-Probe"), "trace_id": sc.TraceID().String(), "span_id": sc.SpanID().String()})
		var record otellog.Record
		record.SetTimestamp(start)
		record.SetObservedTimestamp(time.Now())
		record.SetSeverity(otellog.SeverityInfo)
		record.SetSeverityText("INFO")
		record.SetBody(otellog.StringValue(string(body)))
		record.AddAttributes(otellog.String("http.request.method", r.Method), otellog.Int("http.response.status_code", 200), otellog.String("probe", r.Header.Get("X-SSHMCP-Probe")))
		logger.Emit(r.Context(), record)
	})
	mux.Handle("/", otelhttp.NewHandler(handler, "GET /", otelhttp.WithSpanNameFormatter(func(_ string, r *http.Request) string { return r.Method + " /" })))
	server := &http.Server{Addr: ":8080", Handler: mux, ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 60 * time.Second}
	stopped := make(chan os.Signal, 1)
	signal.Notify(stopped, os.Interrupt, syscall.SIGTERM)
	go func() {
		log.Print("hello-go listening on :8080")
		if e := server.ListenAndServe(); e != nil && e != http.ErrServerClosed {
			log.Fatal(e)
		}
	}()
	<-stopped
	shutdown, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if e := server.Shutdown(shutdown); e != nil {
		log.Print(e)
	}
	for _, f := range []func(context.Context) error{lp.Shutdown, mp.Shutdown, tp.Shutdown} {
		if e := f(shutdown); e != nil {
			log.Print(e)
		}
	}
}
