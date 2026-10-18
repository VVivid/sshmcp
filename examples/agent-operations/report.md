# Actual upgrade, rollback, and interrupted-upgrade report

Run `34852ddc73504e6bb2e19a3957b01690` passed `make agent-demo-operations-test` on 2026-10-18 in **481.370 seconds**. Three fresh installed-Codex sessions operated the existing accepted deployment using only `connect`, `disconnect`, and `run_command`. Both private VMs and the final upgraded demo were left running.

The actors received goals and the three-tool documentation. They discovered the application and chose their own changes, build commands, and deployment steps. No application source, prepared release, Compose recipe, skills, memory, or project instructions were supplied. Each actor started in an empty private workspace outside the checkout. The launcher checked CLI/authentication, the required MCP transport, the exact three-tool catalog, and prompt isolation before each session. The default actor timeout remained 1,800 seconds.

## Independent harness result

Every row below passed fresh Go-to-Traefik trace ancestry, twelve trace-correlated Go logs, the unique probe in proxy JSON logs, genuine typed-counter increases, and all five dashboard checks. Traefik/LGTM container identities and named volumes remained unchanged. The original accepted trace and twelve correlated logs were still retrievable after every phase.

| Phase | Application and counters |
| --- | --- |
| Baseline | Original image and prior HTTP state passed the failed-attempt recovery guard. Go counter 22 → 34; Traefik 24 → 36. |
| Upgrade | New image served `/version` as HTTP 200 with exactly `sshmcp-upgrade-34852ddc73504e6bb2e19a3957b01690`, without a newline. Go counter 103 → 115; Traefik 139 → 151. |
| Exact rollback | Original runtime image, all 23 independently captured source/configuration hashes including private `.env`, and prior `/version` status/body were restored exactly. Go counter 39 → 51; Traefik 190 → 202. |
| Interrupted upgrade | New image served `/version` as HTTP 200 with exactly `sshmcp-interrupted-34852ddc73504e6bb2e19a3957b01690`, without a newline. Go counter 71 → 83; Traefik 273 → 285. |

`/` continued to return HTTP 200 with `Hello, world!\n`. The old application also returned those bytes at `/version`; exact rollback restored that observed behavior. Each phase established new counter baselines after any app recreation. The four fresh probe trace IDs and all backend results are retained in [report.json](report.json) and private evidence; stale samples and actor success statements could not satisfy acceptance.

The exact original and rollback image was:

```text
sha256:ec3fb38cbb9dc77e2b4d17db75325696b0b20c6ecfe2cb4eace9c0a77fbd5dc7
```

The first upgrade used `sha256:ee090a336f1073511c4b4fd8bf3a57b644fd9f2d5ed4ed192d25ec1c222e9a78`; the final interrupted upgrade used `sha256:f16a8d61a4341e025a6d8086f8c47b629110ffd1766d6295c1768a5c5ce16f55`. The rollback actor restored the original image without rebuilding. The harness independently compared the runtime image ID and complete selected source/configuration hash sets.

The upgrade actor preserved 24 text snapshots in its private release directory. All 23 independently selected pre-upgrade files matched their original hashes. One additional existing `.json.disabled` text snapshot had no independent pre-upgrade selected hash; its bounded saved bytes, declared hash, and still-present original matched. That weaker extra-file evidence is counted separately.

## Real gateway interruption and original-job recovery

The final actor staged and built its change, then submitted one activation with the supplied key, `wait_ms=10000`, a private receipt line, and a 25-second delay before activation. An independent read-only MCP observer saw the readiness line, the original job running, and the execute call still in flight. Only then did the fixed management operation stop the gateway.

The gateway was unavailable for **4.249797 seconds**. The observer recorded a real `ConnectionResetError`; the actor retained the failed activation tool call. The gateway restarted successfully, and read-only lookup found the same original job still running. Observer recovery needed zero read-only retries and had no cleanup errors.

The actor reconciled using the original key and job ID, then observed that original job complete with exit code zero. It submitted no other execute job before reconciliation. There was exactly one activation tool call, zero visible activation retries, zero duplicate activation results, and exactly one receipt line. The actor drained the complete original output: stdout bytes **0–46**, stderr bytes **0–232**, with consistent final/eof cursors. Stream hashes are recorded in the JSON report.

```text
key: operations-activation-34852ddc73504e6bb2e19a3957b01690
job: b3f91ef41df3a7a98e466b9777e89dcf
```

Internal CLI transport retry counts are not exposed. Zero visible activation retries does not establish zero internal transport retries.

## Actual actor work and friction

| Actor | Seconds | MCP calls | Completed jobs | Failed jobs | Tool/runtime errors |
| --- | ---: | ---: | ---: | ---: | --- |
| Upgrade | 205.567 | 29 | 7 | 4 | 0 / 0 |
| Rollback | 62.876 | 12 | 5 | 1 | 0 / 0 |
| Interrupted upgrade | 146.450 | 25 | 7 | 1 | 3 / 0 |

All three actors closed their connection. The accepted flow had zero human guidance or intervention; failed remote jobs remain counted. Each observed failed job ended with exit code one; no lost job was observed in these actors.

The interrupted actor's three tool errors were distinct: a rejected idempotency conflict on a read-only inspection key already used in another phase, the original activation transport failure, and one read-only lookup during the outage. The actor resubmitted the rejected inspection source under a fresh key; this accounts for its one repeated source submission. Activation was never replayed. Repeated execute keys and duplicate results were zero in all three actors; the other two actors had no repeated source submissions.

The actors' final messages report the completed changes, exact rollback, preserved infrastructure, and closed connections. The interrupted actor specifically reports recovering the original activation without resubmitting it and correcting a historical-data query window. These are actor statements; independent backend and transcript checks establish the accepted outcome. The complete sanitized final messages are in the JSON report. Per-call actor latency is unavailable in the installed JSONL format.

Installed configuration was observed before and after the run as `gpt-6.1-sol`, reasoning effort `low`, with an unchanged configuration fingerprint. The actors used current defaults, without an invocation model override. This configuration observation is separate from runtime model-metadata attestation. The Builder and harness changed no account, model, or global configuration. Available account quota remained unknown.

## Preserved earlier failures and separate recovery

| Attempt | Result |
| --- | --- |
| `5391a1ac81a54712addf539eff58a8ab` | Host sandbox permission failure during Multipass inventory, before any actor. The diagnostic/shutdown attempts also recorded inventory errors. |
| `b2828c88365a47cab7fe808a2056a790` | Upgrade actor completed in 136.954 seconds with 32 MCP calls, seven completed jobs, five failed jobs, and zero tool/runtime errors. New version/image, fresh telemetry, infrastructure, and history checks passed. The preserved-release verifier then rejected the extra existing text snapshot; rollback and interruption actors never ran. Cleanup had no errors. |

The second attempt remains failed. A subsequent verifier correction requires every selected pre-upgrade hash while checking safe extras separately; it does not retroactively pass the attempt.

Before this accepted run, a **separate recorded MCP recovery** `d7f928a7c4ed41429548c068499b9709` restored all 23 original source/configuration files, the exact original runtime image without rebuilding, and original HTTP behavior. Ten operator jobs completed with zero MCP transport errors and no cleanup errors. Fresh telemetry, original historical data, infrastructure identity, and the failed-baseline guard passed. All 211 files of the failed evidence stayed unchanged; the failed image/source and baseline guard were preserved. No actors ran during recovery, and it did not advance the ready pointer.

That recovery was outside the accepted three-actor run. The accepted run independently checked the recovered baseline before launching its first actor. All prior raw attempts, recovery transcripts, and release snapshots remain private.

## Actual source and access

[source/](source/) contains 22 actual final files: twenty unchanged files from the accepted sanitized export and two actual Python programs from a separate bounded read-only MCP capture. The primary exporter had altered their `password = dict(...)` expressions even though they contained no literal credential. The supplemental capture matched their accepted private hashes, passed Python syntax checks, and contained no known runtime credentials. All 22 published file contents match actual private originals; [source-manifest.json](source-manifest.json) distinguishes the collections and retains the primary redaction hashes. The original private export remains unchanged.

The Go source was copied as captured, without formatting or rewriting. Shell-script modes were normalized to executable because original modes were not captured. Private `.env`, the non-source `.json.disabled` file, image archives, rollback snapshots, receipts, caches, logs, and volumes are excluded. The inherited source README, verification programs, and saved verification JSON describe actor checks; packaging did not rerun them. Independent acceptance came from the repository evaluator.

Private application: <http://127.0.0.1:18090/>. Grafana: <http://127.0.0.1:13000/d/sshmcp-hello>. Credentials are excluded from this snapshot; local access notes remain in the gitignored `.lab/demo/access.json`, mode `0600`.

All 25 files of the previous [agent-demo snapshot](../agent-demo/README.md) retain their pre-run hashes. Management SSH handled infrastructure/lifecycle and the fixed gateway fault; application operations and source reads used MCP.

This is one accepted private Ubuntu ARM64 operations run, using administrator-authorized commands. Public HTTPS exposure, production readiness, high availability, and behavior on other hosts remain unverified.

## Dedicated independent testing

A separate dedicated Tester **passed** the post-Translation audit and live checks: stop 8.577s, resume 84.134s, exact deployment persistence, fresh/historical telemetry, and 16 overlapping warmed sleep-2 jobs drained in 3.370s. All 16 benchmark connections closed; transport/tool/cleanup errors were zero. Both VMs remain running. See [independent-verification.md](independent-verification.md) and [its JSON evidence](independent-verification.json) for measurements and limits.
