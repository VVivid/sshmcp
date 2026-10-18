# Independent verification: PASS

A dedicated Tester independently audited the accepted three-agent run and completed one guarded stop/resume cycle plus one warmed 16-job benchmark on the existing private deployment. No actors or outage were rerun and no application changes were made. Both owned VMs and the final demo remain running.

## Recorded operation and source

Recalculated actor audits match the published counts. Three distinct empty-workspace sessions exposed only `connect`, `disconnect`, and `run_command`; preflight, authenticated catalogs, isolation settings, and actual JSONL tool actions agree. Failed jobs and the interrupted actor's three tool errors remain counted.

The raw fault/observer/actor transcripts prove a 4.250-second real gateway outage, the original failed activation acknowledgement, lookup and completion of that same job, complete stdout/stderr cursor coverage, one receipt line, and no activation replay. Rollback restored the original runtime image and every captured original source/configuration hash. Fresh connected traces, correlated logs, proxy JSON logs, increased counters, dashboard queries, and historical telemetry passed in each recorded phase. Both earlier failed attempts remain failed.

All 22 published source files match their private original hashes and bytes: 20 primary-export files and two bounded supplemental MCP captures. Known current runtime credentials are absent from the public snapshot, `.env` is excluded, and all 25 previous `agent-demo` files retain their recorded hashes.

## Live persistence and concurrency

`make agent-demo-down` completed in **8.577 seconds**; independent inventory verified both VMs `Stopped`. `make agent-demo-up` completed in **84.134 seconds**. Afterwards, all three container identities and image IDs, source/configuration hashes including private `.env`, and named volumes were unchanged. The exact final `/version` marker and root response survived. New connected proxy/app spans, correlated app logs, proxy JSON logs, both increasing counters, dashboard queries, and the original historical trace/logs passed.

One benchmark warmed 16 MCP connections with 16 commands, then dispatched 16 `sleep 2` jobs. All 16 guest execution intervals overlapped; dispatch through final output drain took **3.370 seconds**, below the 8-second limit. During the 16-way concurrent warm-up, command completion median was **1.955 seconds** (the helper's ninth sorted value of 16), maximum **2.120 seconds**. All exact job outputs passed. There were **119 tool calls** (16 connects, 32 executes, 55 statuses, 16 disconnects), zero transport/tool errors, zero automatic retries, and zero cleanup errors. All 16 connections returned `closed`.

Final checks confirm both VMs running with no host mounts, private loopback port publication, the expected pinned LGTM image actually running Linux ARM64, and an exact three-tool catalog. Tester before/after fingerprints preserve credentials, keys, pins, account state, and installed configuration; logical owner and baseline fields match. Private evidence directories use `0700`, files `0600`.

## Evidence and limits

Full raw evidence is private at `.lab/results/20261018T232001Z-cp10-independent-tester-89558f49281441ceaa27eaa909cf7ffb`. [The sanitized JSON](independent-verification.json) records the assertions and exact measurements. Lifecycle output/timing, MCP input/output/transcripts, benchmark jobs, before/after deployment inspection, source hashes, protected fingerprints, telemetry backend responses, and final VM state are retained there.

This is one local private setup and one benchmark. Concurrent warm-up times are not serial steady-state command latency or evidence of speedup over an older benchmark. Status-request latency includes intentional `wait_ms` waits up to 1000 ms; it is not compared to older nonblocking request p95. Installed configuration fingerprints do not attest runtime actor models or quota. Two audit-script comparison/envelope errors were corrected before runtime checks, without application changes; their notes and partial private evidence remain preserved. Public HTTPS, production readiness, high availability, and other hosts remain unverified.
