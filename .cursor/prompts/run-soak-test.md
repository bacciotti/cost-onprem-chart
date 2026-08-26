# Run / Monitor a Soak Test on a Personal Hypervisor

Execute or check on a long-running soak test (`tests/suites/performance/test_soak.py`,
`SOAK-*` tests) via `scripts/soak-loop.sh` on a personal lab hypervisor (e.g.
`ocp-edge94`). This is a **different access pattern from a normal OpenShift
cluster connection** — see `@connect-cluster` for that. Here you SSH directly
into the hypervisor VM that hosts the cluster, not the cluster API.

Full background, rationale, and the incident history behind every gotcha
below: `COST-7634-soak-testing.md` (in the workspace root, not this repo).

## Why a Hypervisor Instead of Jenkins

Soak runs (24h burn-in, 7-day full soak) exceed typical Jenkins job timeouts
and need to survive the CI executor being recycled. Running `soak-loop.sh`
directly on the hypervisor via SSH, daemonized with `nohup`, avoids both
problems and gives S3 checkpoint persistence for remote monitoring.

## 1. Connect

```bash
ssh root@<hypervisor-fqdn>          # e.g. ocp-edge94.lab.eng.tlv2.redhat.com
cd /path/to/cost-onprem-chart       # find the existing checkout — don't assume ~/cost-onprem-chart
```

The hypervisor is a separate machine from the OpenShift cluster it hosts.
`oc`/`kubectl` commands run from here still need `KUBECONFIG` pointed at the
cluster (check for an existing export in the shell, or `~/clusterconfigs/`).

## 2. Check the Python Version Before Doing Anything Else

```bash
python3 --version   # if < 3.10, find a newer one:
ls /usr/bin/python3.1* 2>/dev/null
```

Several test files use PEP 604 `X | None` type hints that raise a cryptic
`TypeError` at pytest **collection** time (not an import error, so it's easy
to misdiagnose) on Python < 3.10. `run-pytest.sh` has a fast-fail guard for
this and will print available candidates, but check up front to save a
cycle. On `ocp-edge94`, default `python3` is 3.9 and `python3.12` is
available — always pass `PYTHON=python3.12` explicitly.

`tests/.venv` is bound to whichever interpreter created it. If tests were
previously run on this host with the wrong interpreter, `rm -rf tests/.venv`
before switching — it will not rebuild itself against a different Python
automatically.

## 3. Launch a Run

Use the same `S3_BUCKET`/`S3_ENDPOINT` you already have configured for
publishing results (see `docs/performance/OBSERVABILITY.md`):

```bash
export S3_BUCKET="<your-teams-perf-bucket>"
export S3_ENDPOINT="<your-teams-s3-endpoint>"
```

Pick one based on what you're validating:

```bash
# Quick harness validation (~1 hour, 4x ~15min condensed iterations)
SOAK_TESTS=true SOAK_CONDENSED=true PYTHON=python3.12 \
  ./scripts/soak-loop.sh --iterations 4 --iteration-hours 0.25 --background \
    --s3-bucket "${S3_BUCKET}" --s3-endpoint "${S3_ENDPOINT}"

# 24-hour burn-in (24x 1-hour iterations)
SOAK_TESTS=true SOAK_DURATION_HOURS=1 PYTHON=python3.12 \
  ./scripts/soak-loop.sh --days 1 --background \
    --s3-bucket "${S3_BUCKET}" --s3-endpoint "${S3_ENDPOINT}"

# Full 7-day soak (168x 1-hour iterations)
SOAK_TESTS=true SOAK_DURATION_HOURS=1 PYTHON=python3.12 \
  ./scripts/soak-loop.sh --days 7 --background \
    --s3-bucket "${S3_BUCKET}" --s3-endpoint "${S3_ENDPOINT}"
```

`--background` daemonizes via `nohup` + `disown` (no `screen`/`tmux` needed —
neither is installed on these hypervisors) and prints a PID/log/stop-file
location, then returns immediately. It's safe to exit the SSH session right
after.

If `--background` exits with an error shortly after launch, that's a **real**
failure (missing `SOAK_TESTS`, unreachable cluster, etc.) — it waits a few
seconds and verifies the child survived before declaring success, so don't
just retry blindly; read the printed log tail first.

## 4. Check Status — Correctly

**Do not assume the process is hung just because it looks quiet.** SOAK-001
sleeps between periodic actions (uploads every N minutes, queries every N
minutes, metrics every ~60s) and only prints progress via `print()`, which
pytest doesn't stream live to the log — so long stretches with no new log
lines and `0% CPU` in `top` are completely normal, not evidence of a hang.

```bash
# Is the loop still running?
cat /tmp/soak-loop.pid && kill -0 $(cat /tmp/soak-loop.pid) && echo "running"

# Find the actual pytest process and how long it's been running
ps auxf | grep -E "soak-loop|pytest" | grep -v grep

# Compare elapsed time to the configured iteration length before assuming a hang
ps -o pid,etime,cmd -p <pytest-pid>
tr '\0' '\n' < /proc/<pytest-pid>/environ | grep SOAK_DURATION_HOURS
```

A pytest process running for ~55-65 minutes with `SOAK_DURATION_HOURS=1` is
on schedule, not stuck. Only treat it as hung if elapsed time meaningfully
exceeds `SOAK_DURATION_HOURS * 2 + overhead`, or if the process has actually
exited (check `ps`) while the wrapper script is still alive without
progressing to the next iteration.

```bash
# Recent log tail — never tail -f, just a bounded read
tail -60 /tmp/soak-loop.log

# Local checkpoint / iteration history for this run
RUN_DIR=tests/soak-runs/<run-id>
ls "${RUN_DIR}/checkpoints/"
cat "${RUN_DIR}/checkpoints/checkpoint-latest.json"
```

## 5. Verify S3 Checkpoints — Use `scripts/s3-upload.py`, Not Raw `aws` CLI

The bucket is anonymous/public on a non-AWS S3 endpoint with an untrusted
TLS cert, and this lab has broken IPv6 routing to it. A bare `aws s3 ls`/`cp`
will behave differently — or just silently fail — depending on whatever AWS
CLI config/credentials happen to be on your machine. Always use the
project's helper, which bakes in anonymous signing, TLS bypass, and
IPv4-only resolution (see step 3 for `S3_BUCKET`/`S3_ENDPOINT`):

```bash
python3 scripts/s3-upload.py ls \
  "s3://${S3_BUCKET}/cost-onprem-performance/<run-id>/" \
  --endpoint-url "${S3_ENDPOINT}"

python3 scripts/s3-upload.py cp \
  "s3://${S3_BUCKET}/cost-onprem-performance/<run-id>/checkpoint-latest.json" - \
  --endpoint-url "${S3_ENDPOINT}"
```

**All soak/perf artifacts are rooted under the shared `cost-onprem-performance/`
prefix** in that bucket — the same one every other perf suite uses via
`perf-observability.sh`'s `S3_PREFIX` convention. Never invent a new
top-level prefix (e.g. a `soak-runs/` folder) for a new suite; if you're
adding S3 publishing to a new test type, reuse this prefix.

## 6. Stop the Run

```bash
# Graceful — finishes the current iteration, then exits
touch /tmp/soak-stop

# Force kill (last resort — skips the final summary)
kill $(cat /tmp/soak-loop.pid)
```

## 7. When It's Done

```bash
# No PID file left behind means it exited cleanly (checked, not assumed)
[ -f /tmp/soak-loop.pid ] && echo "still running" || echo "exited"

tail -40 /tmp/soak-loop.log   # look for "=== Soak Loop Complete ===" and pass/fail counts
```

Cross-check the published `final-results.json` against the log's own summary
— don't trust one source alone:

```bash
python3 scripts/s3-upload.py cp \
  "s3://${S3_BUCKET}/cost-onprem-performance/<run-id>/final-results.json" - \
  --endpoint-url "${S3_ENDPOINT}"
```

## Common Failure Patterns (Already Fixed, but Know the Symptoms)

| Symptom | Cause | Status |
|---|---|---|
| `TypeError: unsupported operand type(s) for \|: 'type' and 'NoneType'` at pytest collection | Python < 3.10 building the test venv | Guarded in `run-pytest.sh`; use `PYTHON=python3.12` up front |
| `WARN S3 upload requires python3 with boto3` at launch, checkpoints never appear | `boto3` not yet available before the test venv is built on a fresh checkout | Fixed — `soak-loop.sh` resolves the S3-capable interpreter lazily per call, not once at startup |
| `SOAK-002 memory leak detected` on a pod that looks fine on inspection | Single-hour two-point extrapolation amplifying normal noise/one-time JVM warm-up by 24x | Fixed — universal 50MB absolute-growth floor + requires 2 consecutive iterations before failing |
| Loop dies silently right after `--background`, `soak-loop.pid` missing moments later | Pre-flight failure (missing `SOAK_TESTS`, unreachable cluster) during fast daemonized startup | Fixed — launch now waits and prints the actual pre-flight error instead of declaring success |
| `s3-upload.py ls` on a prefix returns nothing, or collapses to one `PRE <same-prefix>/` line | Trailing slash stripped before a delimited listing, collapsing all nested keys into one CommonPrefix | Fixed in `s3-upload.py` |

## Terminal Discipline Reminder

This workflow involves a genuinely long-running background process — that
does **not** change the terminal rules. Don't `tail -f` the log, don't loop
polling `ps`/`kill -0` in a shell `while` loop, and don't re-run the same
status check repeatedly in a row. Take one bounded snapshot, reason about
elapsed time vs. configured duration, and only check again after a
meaningful interval has actually passed.
