# Actual fresh-agent deployment and repair report

Accepted real exercise on 2026-10-17, run `c94c230b79564965bf98acd9600e7427`, in 900.061s. The sixth fresh deployment attempt passed; the five earlier attempts remain failed. Both private VMs and the demo were left running.

The deployment and repair used separate fresh installed-agent sessions, empty temporary directories, the normal stored account/model defaults, and only `connect`, `run_command`, and `disconnect`. No application source, Compose files, installation recipes, skills, memory, or project instructions were supplied to either actor. The repair prompt was exactly:

> Telemetry has stopped arriving; diagnose and repair it.

## Independent result

| Phase | Observed result |
| --- | --- |
| Deployment | Fresh proxy-to-Go span ancestry, 12 correlated Go logs, matching proxy JSON log, and all five dashboard checks passed. Go and proxy counters each increased 35 → 47 after 12 requests. |
| Controlled fault | The operator recreated only the app with a deliberately unreachable generic OTLP endpoint. For the complete 120-second window, Go spans/logs stayed at zero and the Go counter stayed 47 → 47. HTTP worked, with 24 fresh proxy spans and matching proxy JSON. |
| Fresh repair | Fresh ancestry, 12 correlated Go logs, matching proxy JSON, and all five dashboard checks passed again. The Go counter increased 35 → 47 and proxy counter 95 → 107 after 12 new requests. |

The two positive phases use distinct trace IDs. Grafana backend responses, operator transcripts, prompts, container inspection, original source bodies, and full actor feedback stay in private local evidence. Exit status and actor claims alone were insufficient.

## Work and recovery

| Actor | Seconds | MCP calls | Completed jobs | Failed jobs | Lost jobs | Tool/runtime errors |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| deployment-actor | 562.666 | 90 | 23 | 5 | 2 | 0/0 |
| repair-actor | 116.817 | 25 | 9 | 0 | 0 | 0/0 |

In-run human guidance was zero. The accepted deployment recovered on its own after five failed jobs and two lost shells; the repair had no failed/lost jobs. Repeated execute keys, exact repeated source submissions, and duplicate results were zero in both successful actors. These counts do not erase earlier failed attempts or imply that no recovery was needed.

The warmed benchmark dispatched 16 real `sleep 2` jobs. All guest intervals overlapped; dispatch through final output drainage took 2.136538s against an 8-second limit. Its 628 MCP calls had median 0.004219s, p95 0.065954s, and maximum 0.128671s. Warm command median/max were 0.145786/0.155272s. Transport errors and automatic transport retries were zero. Actor per-call timing is not available in the installed JSONL format.

## Five preserved failed attempts

| Attempt | Actor seconds | Completed MCP calls | Why acceptance failed |
| --- | ---: | ---: | --- |
| 1 | 13.791 | 0 | CLI transport host unavailable; zero usable MCP work |
| 2 | 550.269 | 92 | Audit rejected successful cleanup of already-lost idle connections |
| 3 | 580.483 | 89 | Counter freshness join mismatched reserved metadata labels |
| 4 | 524.466 | 104 | Dashboard checker rejected valid scalar textbox raw interpolation |
| 5 | 518.863 | 82 | Installed CLI account usage limit interrupted the turn |

Every failed attempt saved its evidence and stopped both VMs with no cleanup errors. None reached controlled fault or a troubleshooting actor. Attempts three and four reached independent deployment checks; the others did not. The fifth transcript was interrupted by account quota and includes an accepted job without a later terminal observation. Full run IDs, job states, recorded errors, and benchmark times are in [report.json](report.json).

Between attempts, engineering corrected the CLI transport isolation, cleanup audit, typed-counter freshness join, and bounded Grafana macro/textbox rendering. Standalone read-only CLI/catalog and backend diagnostics checked those narrow corrections. One earlier positive diagnostic failed on an unsupported Grafana time macro; later diagnostics passed against unchanged preserved applications. The CLI preflight used a separate read-only model session; the catalog/backend probes ran no model. Diagnostics are not accepted exercise runs and do not retroactively pass earlier attempts. The versioned deployment goal stayed unchanged.

During the accepted run, one separate bounded read-only observation checked the fixed gateway/runner services when buffered actor output appeared delayed. It inspected no application files, changed no services, and provided no agent guidance.

The stored Codex auth file differed at the user continuation boundary after the fifth failure. The Builder changed no credentials or global account/model settings. Raw-file hashing cannot distinguish token rotation from reauthentication; current account-identity hashes establish the continuation baseline. VM management keys, pins, account fixture/global configuration bytes, ownership and recorded baselines matched the protected CP7 record.

## Actual source and access

[source/](source/) contains the actual post-repair application/configuration: 20 primary captured files and the actual `verify.py` collected afterward through a bounded read-only MCP operator. The primary capture and its private report were preserved unchanged. [source-manifest.json](source-manifest.json) distinguishes the two collections and records original/export hashes. None of the 21 published file contents required credential redaction. Private `.env`, compiled output, caches, logs, volumes and data are excluded. File contents are preserved; executable shell-script modes are normalized because the exporter did not capture original modes.

The source README and `verification*.json` are unedited actor artifacts. They describe the actor’s own checks. `verify.py` was collected as source and was not rerun by the packaging step; independent acceptance came from the repository evaluator. The actor reported a successful reboot check; the dedicated Tester separately verified actual demo stop/start below.

Private application: <http://127.0.0.1:18090/>. Grafana: <http://127.0.0.1:13000/d/sshmcp-hello>. Credentials are only in the gitignored `.lab/demo/access.json` with mode `0600`. No public listener, public deployment, or Mac mount was added.

This is one successful private ARM64 exercise after five failures. It does not establish production readiness, high availability, or behavior on other hosts.

## Dedicated independent testing

The dedicated Tester passed actual `agent-demo-down` in 9.465s and
`agent-demo-up` in 75.021s. Both VMs stopped, then resumed from the same disks.
Separate fresh post-resume checks found connected traces, twelve correlated Go
logs, proxy JSON logs, both genuine counters increasing 18 → 30, and all five
dashboard checks. Pre-stop trace, log, and counter history remained retrievable.
Container identities, named volumes, 23 private captured files, and all 21
published source hashes were preserved.

With the stack running, sixteen warmed `sleep 2` jobs all overlapped and drained
in 2.169478s. Their 634 MCP calls had zero transport errors or retries, and all
sixteen connections closed. Live inspection also confirmed private networking,
loopback-only published ports, empty VM mounts, ARM64 images, SDK instrumentation,
protected configuration/identities, and private credentials. Both VMs and the
demo were left running.

The Tester separately audited preserved actor/fault records; it executed the
lifecycle, fresh telemetry, persistence, privacy, and concurrency checks now.
It changed no application files and launched no new model actors. Tester-only
capture corrections are retained in private evidence:
`.lab/results/20261017T210215Z-cp7-independent-tester-206c830dfb5b40c7b14dc2ae5f125d73/`.

## Actor feedback

The deployment actor reported the completed three-service stack, correlated telemetry/dashboard checks, a reboot check, and Docker bridge DNS/tool friction. The repair actor reported finding the unreachable generic endpoint override and restoring export by recreating only the app. Sanitized original feedback is included under each actor in [report.json](report.json), separately from independent evidence.
