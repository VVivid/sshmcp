# Actual agent operations snapshot

Three fresh agents upgraded the existing private demo, rolled back to its exact original image and source, then completed another upgrade across a real gateway outage. The independent harness accepted all three phases on 2026-10-18. Both VMs and the final demo were left running.

Read [report.md](report.md) for observed results, failures, recovery, and limits; [report.json](report.json) holds the detailed sanitized evidence. The separate dedicated Tester [passed the independent audit, stop/resume persistence, telemetry, and warmed concurrency checks](independent-verification.md); [detailed measurements](independent-verification.json) record the results and limits.

To follow the acceptance logic, start at [tools/demo_operations.py](../../tools/demo_operations.py): `main()` calls `run_operation()`, then `exercise()` runs the three phases. `evaluate_phase()` checks HTTP behavior, fresh telemetry, history, and infrastructure; `require_rollback()` compares the original image and source hashes; `validate_interruption()` checks recovery of the original activation job and complete output. Actor isolation and the three-tool preflight live in [tools/demo_actor.py](../../tools/demo_actor.py).

[source/](source/) contains 22 actual post-operations files from the original deployment/repair and subsequent operations actors. [source-manifest.json](source-manifest.json) records their original and published hashes, collection paths, exclusions, and bounds. Twenty files came unchanged from the accepted export. Two Python files came from a separate bounded read-only MCP capture because the generic exporter altered nonliteral credential-loading expressions. Their original hashes match the accepted private capture; known runtime credentials are absent and Python syntax is valid. All 22 published contents match their private source hashes. The private export was preserved.

The [source README](source/README.md), verification programs, and saved verification results are unedited actor artifacts. Private `.env`, credentials, image archives, rollback snapshots, receipts, logs, and volumes are excluded. Verification programs expect the private environment; they were captured as source and were not run during packaging. Build/deploy scripts operate Docker and belong on an appropriate private test host.

The previous [agent-demo snapshot](../agent-demo/README.md) remains unchanged. This exercise covers administrator-authorized operations on one private Ubuntu ARM64 target. Public HTTPS exposure and production readiness remain unverified.
