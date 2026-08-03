"""
RBAC Authorization Performance Tests (COST-7643).

Isolate and measure the insights-rbac contribution to API latency. Every
Koku API call delegates a permission check to the RBAC service; if RBAC is
slow, all API latencies degrade. These tests quantify the overhead and
identify whether RBAC is a bottleneck.

Test IDs:
- PERF-RBAC-001: Baseline RBAC latency isolation
- PERF-RBAC-002: Cache effectiveness (cold vs warm)
- PERF-RBAC-003: Concurrent authorization load (parametrized)
- PERF-RBAC-004: Multi-org scaling (parametrized, conditional)

Usage:
    # All RBAC perf tests
    ./scripts/deploy-test-cost-onprem.sh --perf-only --perf-profile medium --perf-suite rbac

    # Direct pytest
    PERF_PROFILE=medium pytest -m "performance and rbac_perf" tests/suites/performance/
"""

import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest
import requests as _requests

from conftest import ClusterConfig, DatabaseConfig
from utils import exec_in_pod, get_pod_by_label, run_oc_command

from .data_classes import PerformanceResult
from .helpers import (
    PerfResultCollector,
    PerfTimer,
    create_authenticated_session,
)
from .k8s_helpers import calculate_percentiles, capture_pg_stats, diff_pg_stats


# =============================================================================
# Helpers
# =============================================================================


def _rbac_access_url(gateway_url: str) -> str:
    """RBAC permission check endpoint via the Envoy gateway.

    Tests run outside the cluster (Jenkins hypervisor), so ClusterIP
    service DNS is not resolvable. The gateway routes ``/api/rbac/``
    to the RBAC backend, giving us an externally reachable path that
    still isolates RBAC latency from the Koku API path.
    """
    return f"{gateway_url.rstrip('/')}/api/rbac/v1/access/?application=cost-management"


RBAC_VALKEY_DB = 2


def _flush_rbac_cache(namespace: str) -> int:
    """Flush all keys from Valkey DB used by RBAC (Django cache DB 2).

    RBAC's Django cache uses ``redis://…/2`` with its own key format
    (version-prefixed, e.g. ``:1:key``), so we flush the entire DB
    rather than pattern-matching on a prefix.
    """
    pod = get_pod_by_label(namespace, "app.kubernetes.io/component=cache")
    if not pod:
        return -1

    count_result = exec_in_pod(
        namespace, pod,
        ["valkey-cli", "-n", str(RBAC_VALKEY_DB), "DBSIZE"],
        timeout=15,
    )
    before_count = 0
    if count_result:
        try:
            before_count = int(count_result.strip())
        except ValueError:
            pass

    result = exec_in_pod(
        namespace, pod,
        ["valkey-cli", "-n", str(RBAC_VALKEY_DB), "FLUSHDB"],
        timeout=30,
    )
    if result is None:
        return -1
    return before_count


def _count_rbac_cache_keys(namespace: str) -> int:
    """Count keys in Valkey DB used by RBAC (DB 2)."""
    pod = get_pod_by_label(namespace, "app.kubernetes.io/component=cache")
    if not pod:
        return -1

    result = exec_in_pod(
        namespace, pod,
        ["valkey-cli", "-n", str(RBAC_VALKEY_DB), "DBSIZE"],
        timeout=15,
    )
    if result is None:
        return -1
    try:
        return int(result.strip())
    except ValueError:
        return -1


def _measure_latency(
    session: _requests.Session,
    url: str,
    n: int = 100,
    timeout: float = 30.0,
) -> List[float]:
    """Make N sequential GET requests and return latencies in seconds."""
    latencies = []
    for _ in range(n):
        start = time.time()
        try:
            resp = session.get(url, timeout=timeout)
            elapsed = time.time() - start
            latencies.append(elapsed)
            if resp.status_code >= 500:
                print(f"  [latency] {url} returned {resp.status_code}")
        except _requests.RequestException as e:
            latencies.append(time.time() - start)
            print(f"  [latency] {url} error: {e}")
    return latencies


def _measure_latency_concurrent(
    session_factory,
    url: str,
    concurrency: int,
    duration_s: float = 60.0,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Run concurrent GET requests for duration_s seconds.

    session_factory: callable that returns a new requests.Session (one per thread).
    Returns percentile stats + throughput.
    """
    stop_event = threading.Event()
    all_latencies: List[float] = []
    error_count = 0
    lock = threading.Lock()

    def _worker():
        nonlocal error_count
        sess = session_factory()
        local_latencies = []
        local_errors = 0
        while not stop_event.is_set():
            start = time.time()
            try:
                resp = sess.get(url, timeout=timeout)
                elapsed = time.time() - start
                local_latencies.append(elapsed)
                if resp.status_code >= 500:
                    local_errors += 1
            except _requests.RequestException:
                local_latencies.append(time.time() - start)
                local_errors += 1
        with lock:
            all_latencies.extend(local_latencies)
            error_count += local_errors

    threads = []
    for _ in range(concurrency):
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        threads.append(t)

    time.sleep(duration_s)
    stop_event.set()
    for t in threads:
        t.join(timeout=10)

    stats = calculate_percentiles(all_latencies, errors=error_count)
    stats["total_requests"] = len(all_latencies)
    stats["requests_per_second"] = round(len(all_latencies) / max(duration_s, 0.1), 1)
    stats["concurrency"] = concurrency
    return stats


# =============================================================================
# Test Class
# =============================================================================


@pytest.mark.performance
@pytest.mark.rbac_perf
class TestRBACPerf:
    """RBAC authorization performance tests (COST-7643)."""

    @pytest.fixture(autouse=True)
    def setup(self, cluster_config: ClusterConfig, keycloak_config):
        self.namespace = cluster_config.namespace
        self.helm_release = cluster_config.helm_release_name
        self._keycloak_config = keycloak_config
        self._cluster_config = cluster_config

    def _create_session(self) -> _requests.Session:
        return create_authenticated_session(self._keycloak_config)

    # -----------------------------------------------------------------
    # RBAC-001: Baseline latency isolation
    # -----------------------------------------------------------------

    @pytest.mark.timeout(300)
    def test_perf_rbac_001_baseline_isolation(
        self,
        cluster_config: ClusterConfig,
        database_config: DatabaseConfig,
        gateway_url: str,
        perf_timer: PerfTimer,
        perf_result: PerformanceResult,
        perf_collector: PerfResultCollector,
        keycloak_config,
    ):
        """PERF-RBAC-001: Measure RBAC's contribution to API latency.

        Compares direct RBAC access-check latency against end-to-end Koku
        API latency to determine what percentage of total latency comes
        from RBAC permission checks.
        """
        print(f"\n{'='*72}")
        print("PERF-RBAC-001: Baseline RBAC Latency Isolation")
        print(f"{'='*72}\n")

        session = self._create_session()
        rbac_access = _rbac_access_url(gateway_url)
        koku_reports = f"{gateway_url.rstrip('/')}/api/cost-management/v1/reports/openshift/costs/"

        # Warm up: a few requests to prime caches
        for _ in range(5):
            session.get(rbac_access, timeout=30)
            session.get(koku_reports, timeout=30)

        # Capture PG stats during measurement
        pg_before = capture_pg_stats(
            self.namespace,
            database_config.pod_name,
            database_config.database,
            database_config.user,
        )

        # Measure direct RBAC latency (100 sequential calls)
        print("Measuring direct RBAC latency (100 calls)...")
        rbac_latencies = _measure_latency(session, rbac_access, n=100)
        rbac_stats = calculate_percentiles(rbac_latencies)
        print(f"  RBAC direct: p50={rbac_stats['p50']*1000:.1f}ms "
              f"p95={rbac_stats['p95']*1000:.1f}ms "
              f"p99={rbac_stats['p99']*1000:.1f}ms")

        # Measure end-to-end Koku API latency (100 sequential calls)
        print("Measuring end-to-end Koku API latency (100 calls)...")
        koku_latencies = _measure_latency(session, koku_reports, n=100)
        koku_stats = calculate_percentiles(koku_latencies)
        print(f"  Koku API:    p50={koku_stats['p50']*1000:.1f}ms "
              f"p95={koku_stats['p95']*1000:.1f}ms "
              f"p99={koku_stats['p99']*1000:.1f}ms")

        pg_after = capture_pg_stats(
            self.namespace,
            database_config.pod_name,
            database_config.database,
            database_config.user,
        )
        pg_delta = diff_pg_stats(pg_before, pg_after)

        # Calculate RBAC's share
        rbac_pct_p50 = (rbac_stats["p50"] / koku_stats["p50"] * 100) if koku_stats["p50"] > 0 else 0
        rbac_pct_p95 = (rbac_stats["p95"] / koku_stats["p95"] * 100) if koku_stats["p95"] > 0 else 0

        print(f"\n{'='*72}")
        print("RBAC-001 SUMMARY")
        print(f"{'='*72}")
        print(f"RBAC share of API latency: p50={rbac_pct_p50:.0f}%, p95={rbac_pct_p95:.0f}%")
        print(f"PG commits during measurement: {pg_delta.get('xact_commit_delta', '?')}")
        print(f"PG cache hit ratio: {pg_delta.get('cache_hit_ratio', '?')}")

        perf_result.test_id = "PERF-RBAC-001"
        perf_result.metrics = {
            "rbac_direct": rbac_stats,
            "koku_api": koku_stats,
            "rbac_share_pct_p50": round(rbac_pct_p50, 1),
            "rbac_share_pct_p95": round(rbac_pct_p95, 1),
            "pg_stats": pg_delta,
        }
        perf_result.passed = True
        perf_collector.add_result(perf_result)

    # -----------------------------------------------------------------
    # RBAC-002: Cache effectiveness
    # -----------------------------------------------------------------

    @pytest.mark.timeout(300)
    def test_perf_rbac_002_cache_effectiveness(
        self,
        cluster_config: ClusterConfig,
        gateway_url: str,
        perf_timer: PerfTimer,
        perf_result: PerformanceResult,
        perf_collector: PerfResultCollector,
        keycloak_config,
    ):
        """PERF-RBAC-002: Measure cache-hit vs cache-miss latency.

        Flushes RBAC keys from Valkey, measures cold (miss) latency,
        then measures warm (hit) latency. The delta quantifies the
        value of the RBAC cache.
        """
        print(f"\n{'='*72}")
        print("PERF-RBAC-002: Cache Effectiveness")
        print(f"{'='*72}\n")

        session = self._create_session()
        rbac_access = _rbac_access_url(gateway_url)

        # Flush RBAC cache
        deleted = _flush_rbac_cache(self.namespace)
        print(f"Flushed {deleted} rbac:* keys from Valkey")

        if deleted < 0:
            pytest.skip("Could not flush Valkey RBAC cache")

        # Cold (cache miss) measurement
        print("Measuring cold latency (cache miss, 50 calls)...")
        cold_latencies = _measure_latency(session, rbac_access, n=50)
        cold_stats = calculate_percentiles(cold_latencies)
        print(f"  Cold: p50={cold_stats['p50']*1000:.1f}ms "
              f"p95={cold_stats['p95']*1000:.1f}ms")

        cache_keys_after_cold = _count_rbac_cache_keys(self.namespace)
        print(f"  Cache keys after cold run: {cache_keys_after_cold}")

        # Warm (cache hit) measurement — cache should now be populated
        print("Measuring warm latency (cache hit, 50 calls)...")
        warm_latencies = _measure_latency(session, rbac_access, n=50)
        warm_stats = calculate_percentiles(warm_latencies)
        print(f"  Warm: p50={warm_stats['p50']*1000:.1f}ms "
              f"p95={warm_stats['p95']*1000:.1f}ms")

        # Calculate speedup
        speedup_p50 = cold_stats["p50"] / warm_stats["p50"] if warm_stats["p50"] > 0 else 0
        speedup_p95 = cold_stats["p95"] / warm_stats["p95"] if warm_stats["p95"] > 0 else 0
        delta_ms = (cold_stats["p50"] - warm_stats["p50"]) * 1000

        print(f"\n{'='*72}")
        print("RBAC-002 SUMMARY")
        print(f"{'='*72}")
        print(f"Cache speedup: p50={speedup_p50:.1f}×, p95={speedup_p95:.1f}×")
        print(f"Absolute delta (p50): {delta_ms:.1f}ms")
        print(f"Cache keys populated: {cache_keys_after_cold}")

        perf_result.test_id = "PERF-RBAC-002"
        perf_result.metrics = {
            "cold": cold_stats,
            "warm": warm_stats,
            "speedup_p50": round(speedup_p50, 2),
            "speedup_p95": round(speedup_p95, 2),
            "delta_ms_p50": round(delta_ms, 1),
            "cache_keys": cache_keys_after_cold,
        }
        perf_result.passed = True
        perf_collector.add_result(perf_result)

    # -----------------------------------------------------------------
    # RBAC-003: Concurrent load
    # -----------------------------------------------------------------

    @pytest.mark.timeout(600)
    @pytest.mark.parametrize("concurrency", [1, 5, 10, 20, 50])
    def test_perf_rbac_003_concurrent_auth(
        self,
        concurrency: int,
        cluster_config: ClusterConfig,
        database_config: DatabaseConfig,
        gateway_url: str,
        perf_timer: PerfTimer,
        perf_result: PerformanceResult,
        perf_collector: PerfResultCollector,
        keycloak_config,
    ):
        """PERF-RBAC-003: Measure RBAC throughput at varying concurrency.

        Runs N threads for 60 seconds, each hitting the RBAC access
        endpoint. Identifies the concurrency level where latency
        degrades >2× baseline (concurrency=1).
        """
        print(f"\n{'='*72}")
        print(f"PERF-RBAC-003: Concurrent Auth Load (concurrency={concurrency})")
        print(f"{'='*72}\n")

        rbac_access = _rbac_access_url(gateway_url)

        pg_before = capture_pg_stats(
            self.namespace,
            database_config.pod_name,
            database_config.database,
            database_config.user,
        )

        stats = _measure_latency_concurrent(
            session_factory=self._create_session,
            url=rbac_access,
            concurrency=concurrency,
            duration_s=60.0,
        )

        pg_after = capture_pg_stats(
            self.namespace,
            database_config.pod_name,
            database_config.database,
            database_config.user,
        )
        pg_delta = diff_pg_stats(pg_before, pg_after)

        # Get RBAC pod CPU/memory usage
        rbac_pod = get_pod_by_label(self.namespace, "app.kubernetes.io/component=rbac-api")
        pod_metrics = {}
        if rbac_pod:
            result = run_oc_command(
                ["adm", "top", "pod", rbac_pod, "-n", self.namespace, "--no-headers"],
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split()
                if len(parts) >= 3:
                    pod_metrics = {"cpu": parts[1], "memory": parts[2]}

        print(f"\n{'='*72}")
        print(f"RBAC-003 SUMMARY (concurrency={concurrency})")
        print(f"{'='*72}")
        print(f"Requests: {stats['total_requests']} in 60s "
              f"({stats['requests_per_second']} req/s)")
        print(f"Latency: p50={stats['p50']*1000:.1f}ms "
              f"p95={stats['p95']*1000:.1f}ms "
              f"p99={stats['p99']*1000:.1f}ms")
        print(f"Errors: {stats.get('errors', 0)}")
        if pod_metrics:
            print(f"RBAC pod: CPU={pod_metrics.get('cpu', '?')}, "
                  f"Memory={pod_metrics.get('memory', '?')}")
        print(f"PG commits: {pg_delta.get('xact_commit_delta', '?')}, "
              f"cache hit: {pg_delta.get('cache_hit_ratio', '?')}")

        perf_result.test_id = f"PERF-RBAC-003[{concurrency}]"
        perf_result.metrics = {
            "concurrency": concurrency,
            "latency": stats,
            "rbac_pod": pod_metrics,
            "pg_stats": pg_delta,
        }
        perf_result.passed = True
        perf_collector.add_result(perf_result)

    # -----------------------------------------------------------------
    # RBAC-004: Multi-org scaling (conditional)
    # -----------------------------------------------------------------

    @pytest.mark.timeout(600)
    @pytest.mark.parametrize("org_count", [1, 5, 10])
    def test_perf_rbac_004_multi_org_scaling(
        self,
        org_count: int,
        cluster_config: ClusterConfig,
        gateway_url: str,
        perf_timer: PerfTimer,
        perf_result: PerformanceResult,
        perf_collector: PerfResultCollector,
        keycloak_config,
    ):
        """PERF-RBAC-004: Measure RBAC latency with varying org count.

        Checks how many tenants exist in the RBAC database, then
        measures access-check latency. If RBAC queries are properly
        scoped per-tenant, latency should be flat regardless of org count.

        This test is informational — it measures the current state rather
        than provisioning additional orgs (which would require Keycloak
        sync and is destructive to the test environment).
        """
        print(f"\n{'='*72}")
        print(f"PERF-RBAC-004: Multi-Org Scaling Check (target={org_count})")
        print(f"{'='*72}\n")

        # Count current tenants in RBAC database
        db_pod = get_pod_by_label(self.namespace, "app.kubernetes.io/component=database")
        if not db_pod:
            pytest.skip("Database pod not found")

        from utils import execute_db_query
        rbac_db_user = "postgres"
        tenant_rows = execute_db_query(
            self.namespace, db_pod,
            "costonprem_rbac",
            rbac_db_user,
            "SELECT count(*) FROM api_tenant;",
        )
        current_tenants = 0
        if tenant_rows and tenant_rows[0]:
            try:
                current_tenants = int(tenant_rows[0][0])
            except (ValueError, IndexError):
                pass

        print(f"Current tenant count in RBAC DB: {current_tenants}")

        if current_tenants < org_count:
            pytest.skip(
                f"Need {org_count} orgs but only {current_tenants} exist. "
                f"Multi-org provisioning requires Keycloak sync."
            )

        session = self._create_session()
        rbac_access = _rbac_access_url(gateway_url)

        # Measure RBAC latency at current org count
        print(f"Measuring RBAC latency with {current_tenants} tenants (100 calls)...")
        latencies = _measure_latency(session, rbac_access, n=100)
        stats = calculate_percentiles(latencies)

        # Get RBAC table sizes
        table_sizes = {}
        for table in ["api_tenant", "api_principal", "management_group", "management_policy", "management_role"]:
            size_result = execute_db_query(
                self.namespace, db_pod,
                "costonprem_rbac",
                rbac_db_user,
                f"SELECT count(*) FROM {table};",
            )
            if size_result and size_result[0]:
                try:
                    table_sizes[table] = int(size_result[0][0])
                except (ValueError, IndexError):
                    pass

        print(f"\n{'='*72}")
        print(f"RBAC-004 SUMMARY (tenants={current_tenants}, target={org_count})")
        print(f"{'='*72}")
        print(f"Latency: p50={stats['p50']*1000:.1f}ms "
              f"p95={stats['p95']*1000:.1f}ms "
              f"p99={stats['p99']*1000:.1f}ms")
        print(f"RBAC table sizes: {table_sizes}")

        perf_result.test_id = f"PERF-RBAC-004[{org_count}]"
        perf_result.metrics = {
            "org_count_target": org_count,
            "actual_tenants": current_tenants,
            "latency": stats,
            "table_sizes": table_sizes,
        }
        perf_result.passed = True
        perf_collector.add_result(perf_result)
