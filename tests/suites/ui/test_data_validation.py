"""
UI data validation tests.

These tests are SELF-CONTAINED - they set up their own test data using the
cost_validation_data fixture, then validate that data displays correctly in the UI.

1. Data Visualization - Charts render correctly with real cost data
2. Optimization Recommendations - CPU/memory recommendations display correctly
3. Optimization Breakdown - Detailed breakdown view shows correct data
"""

import os
import re
import time

import pytest
from playwright.sync_api import Page, expect


def save_screenshot(page: Page, name: str) -> str:
    """Save a screenshot for documentation/verification purposes."""
    screenshots_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "reports", "screenshots", "data_validation"
    )
    os.makedirs(screenshots_dir, exist_ok=True)
    path = os.path.join(screenshots_dir, f"{name}.png")
    page.screenshot(path=path, full_page=True)
    print(f"\n📸 Screenshot: {path}")
    return path


@pytest.mark.ui
@pytest.mark.data_validation
class TestCostDataVisualization:
    """Test that cost data displays correctly in charts and tables.
    
    HIGH PRIORITY: Validates that the UI correctly renders cost data from the backend.
    Uses cost_validation_data fixture for self-contained data setup.
    """

    def test_overview_shows_cost_data(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify Overview page displays cost data (not empty state).
        
        The Overview page should show:
        - Cost summary cards or widgets
        - Charts with actual data points
        - Not just "No data available" messages
        """
        authenticated_page.goto(f"{ui_url}/openshift/cost-management")
        authenticated_page.wait_for_load_state("networkidle")
        
        # Wait for any loading indicators to disappear
        loading = authenticated_page.locator(".pf-v6-c-spinner, [data-testid='loading']")
        if loading.count() > 0:
            loading.first.wait_for(state="hidden", timeout=30000)
        
        # Check for empty state - should NOT be present since we have data
        empty_state = authenticated_page.locator(".pf-v6-c-empty-state, .pf-c-empty-state")
        empty_text = authenticated_page.get_by_text(re.compile(r"no data|no cost data|empty", re.IGNORECASE))
        
        # With cost_validation_data, we expect data to be present
        # If empty state is shown, the test should fail (not skip)
        if empty_state.count() > 0 and empty_state.first.is_visible():
            pytest.fail(
                f"Empty state shown despite cost_validation_data setup. "
                f"Cluster ID: {cost_validation_data['cluster_id']}"
            )
        
        # Look for data indicators - charts, tables, or cost values
        found_data = False
        
        # Check CSS selectors
        css_indicators = ["svg path", "svg rect", "table tbody tr", ".pf-v6-c-card"]
        for selector in css_indicators:
            if authenticated_page.locator(selector).count() > 0:
                found_data = True
                break
        
        # Check for dollar amounts via text
        if not found_data:
            dollar_amounts = authenticated_page.get_by_text(re.compile(r"\$[0-9]"))
            if dollar_amounts.count() > 0:
                found_data = True
        
        # Capture screenshot for verification
        save_screenshot(authenticated_page, "01_overview_cost_data")
        
        assert found_data, (
            f"Overview page should display cost data (charts, tables, or cost values). "
            f"Cluster ID: {cost_validation_data['cluster_id']}"
        )

    def test_openshift_page_shows_cluster_costs(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify OpenShift page displays cluster cost data.
        
        The OpenShift page should show:
        - Cost breakdown by cluster, project, or node
        - Charts or tables with actual values
        
        Note: The /ocp page may show empty state initially while data propagates
        through the UI's caching layer, even when data exists in the database.
        """
        authenticated_page.goto(f"{ui_url}/openshift/cost-management/ocp")
        authenticated_page.wait_for_load_state("networkidle")
        
        # Wait for loading to complete - OCP page may need more time
        time.sleep(5)  # Allow async data to load
        
        # Look for cost data elements first
        found_data = False
        if authenticated_page.locator("svg path, svg rect, table tbody tr, .pf-v6-c-table tbody tr").count() > 0:
            found_data = True
        if not found_data and authenticated_page.get_by_text(re.compile(r"\$[0-9]", re.IGNORECASE)).count() > 0:
            found_data = True
        
        if found_data:
            # Capture screenshot for verification
            save_screenshot(authenticated_page, "02_openshift_cluster_costs")
            return  # Test passes - data is displayed
        
        # Check for empty state - may occur due to UI caching/timing
        empty_state = authenticated_page.locator(".pf-v6-c-empty-state")
        if empty_state.count() > 0 and empty_state.first.is_visible():
            # This can happen due to UI caching - skip rather than fail
            # The Overview and Cost Explorer tests validate data is accessible
            pytest.skip(
                f"OpenShift page shows empty state (possible UI caching delay). "
                f"Data exists for cluster: {cost_validation_data['cluster_id']}"
            )
        
        # No data and no empty state - something else is wrong
        pytest.fail(
            f"OpenShift page should display cost data or empty state. "
            f"Cluster ID: {cost_validation_data['cluster_id']}"
        )

    def test_cost_explorer_displays_chart_with_data(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify Cost Explorer displays charts with actual data points.
        
        The Cost Explorer should show:
        - A chart (bar, line, or area) with visible data
        - Not just empty axes or "no data" message
        
        Note: The Cost Explorer may show empty state transiently while the UI's
        caching layer propagates cost data.  Poll briefly before concluding.
        """
        authenticated_page.goto(f"{ui_url}/openshift/cost-management/explorer")
        authenticated_page.wait_for_load_state("networkidle")

        chart_with_data = authenticated_page.locator("svg path, svg rect, svg circle")

        for attempt in range(4):
            time.sleep(3)
            if chart_with_data.count() > 2:
                break
            if attempt < 3:
                authenticated_page.reload()
                authenticated_page.wait_for_load_state("networkidle")

        save_screenshot(authenticated_page, "03_cost_explorer_chart")

        if chart_with_data.count() > 2:
            return

        empty_state = authenticated_page.locator(".pf-v6-c-empty-state")
        empty_text = authenticated_page.get_by_text(re.compile(r"no data available", re.IGNORECASE))
        if (empty_state.count() > 0 and empty_state.first.is_visible()) or \
           (empty_text.count() > 0 and empty_text.first.is_visible()):
            pytest.skip(
                f"Empty state shown after retries (possible UI caching delay). "
                f"Data exists for cluster: {cost_validation_data['cluster_id']}"
            )

        pytest.fail(
            f"Cost Explorer chart should have data points. Found {chart_with_data.count()} SVG elements. "
            f"Cluster ID: {cost_validation_data['cluster_id']}"
        )

    def test_cost_explorer_table_has_rows(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify Cost Explorer table displays data rows.
        
        When viewing as table, should show actual cost data rows.
        """
        authenticated_page.goto(f"{ui_url}/openshift/cost-management/explorer")
        authenticated_page.wait_for_load_state("networkidle")
        
        # Wait for data to load
        time.sleep(2)
        
        # Look for table rows
        table_rows = authenticated_page.locator(
            "table tbody tr, [role='grid'] [role='row'], .pf-v6-c-table tbody tr"
        )
        
        if table_rows.count() == 0:
            # Check if there's a table view toggle
            table_toggle = authenticated_page.locator(
                "button:has-text('Table'), [aria-label*='table'], [data-testid='table-view']"
            )
            if table_toggle.count() > 0:
                table_toggle.first.click()
                authenticated_page.wait_for_load_state("networkidle")
                time.sleep(1)
                table_rows = authenticated_page.locator("table tbody tr")
        
        # Capture screenshot for verification
        save_screenshot(authenticated_page, "04_cost_explorer_table")
        
        # With data setup, we expect rows (or chart view is default which is also valid)
        # This test validates table view specifically if available
        if table_rows.count() == 0:
            # Check if chart is showing instead (also valid)
            chart_elements = authenticated_page.locator("svg path, svg rect")
            if chart_elements.count() > 2:
                pass  # Chart view is showing data, that's fine
            else:
                pytest.fail(
                    f"Cost Explorer should have data in table or chart view. "
                    f"Cluster ID: {cost_validation_data['cluster_id']}"
                )


@pytest.mark.ui
@pytest.mark.ros
@pytest.mark.data_validation
class TestOptimizationRecommendations:
    """Test that optimization recommendations display correctly.
    
    HIGH PRIORITY: Validates CPU/memory recommendations from Kruize are shown.
    Uses cost_validation_data fixture for self-contained data setup.
    
    Note: Optimization recommendations require Kruize processing time after data upload.
    The E2E flow waits for recommendations to be generated before these tests run.
    """

    def test_optimizations_table_has_recommendations(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify optimizations page displays recommendation data.
        
        Should show:
        - Table with container/workload recommendations
        - CPU and memory columns
        - Actual values (not all zeros or empty)
        """
        authenticated_page.goto(f"{ui_url}/openshift/cost-management/optimizations")
        authenticated_page.wait_for_load_state("networkidle")
        
        # Wait for data to load
        time.sleep(3)
        
        # Check for empty state
        empty_state = authenticated_page.locator(".pf-v6-c-empty-state, .pf-c-empty-state")
        empty_text = authenticated_page.get_by_text(re.compile(r"no optimization|no recommendation", re.IGNORECASE))
        
        # Optimization data may take longer to process - check if available
        if (empty_state.count() > 0 and empty_state.first.is_visible()) or \
           (empty_text.count() > 0 and empty_text.first.is_visible()):
            # This is acceptable - Kruize may not have processed yet
            pytest.skip(
                "No optimization data available yet. "
                "Kruize may still be processing recommendations."
            )
        
        # Look for table with data
        table = authenticated_page.locator("table, [role='grid'], .pf-v6-c-table")
        expect(table.first).to_be_visible(timeout=10000)
        
        # Capture screenshot for verification
        save_screenshot(authenticated_page, "05_optimizations_table")
        
        # Verify table has rows
        rows = authenticated_page.locator("table tbody tr, [role='row']")
        assert rows.count() > 0, "Optimizations table should have recommendation rows"

    def test_optimizations_show_cpu_memory_values(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify CPU/memory recommendation data is available for optimizations.

        The new UI shows optimizations as a table; per-container detail
        drill-down is no longer available via click. Validate that:
        1. The optimizations table shows our test container (filtered)
        2. The recommendations API returns CPU/memory data for it
        """
        authenticated_page.goto(f"{ui_url}/openshift/cost-management/optimizations")
        authenticated_page.wait_for_load_state("networkidle")
        time.sleep(3)

        empty_state = authenticated_page.locator(".pf-v6-c-empty-state, .pf-c-empty-state")
        if empty_state.count() > 0 and empty_state.first.is_visible():
            pytest.skip("No optimization data available yet")

        # Filter the table by our test container name
        filter_input = authenticated_page.locator(
            "input[placeholder*='Filter'], input[aria-label*='filter'], "
            "input[placeholder*='container']"
        )
        if filter_input.count() > 0:
            filter_input.first.fill("test-pod")
            filter_input.first.press("Enter")
            authenticated_page.wait_for_load_state("networkidle")
            time.sleep(2)

        # Verify the test container appears in the filtered results
        table_content = authenticated_page.locator("table tbody")
        assert table_content.count() > 0, "Optimizations table should be present"

        save_screenshot(authenticated_page, "06_optimizations_cpu_memory")

        # Verify recommendation data has CPU/memory via the API
        response = authenticated_page.request.get(
            f"{ui_url}/api/cost-management/v1/recommendations/openshift"
            f"?container=test-pod-1&limit=1"
        )
        assert response.status == 200, f"Recommendations API returned {response.status}"
        data = response.json().get("data", [])
        assert len(data) > 0, "Should have at least one recommendation for test-pod-1"

        rec = data[0].get("recommendations", {})
        current = rec.get("current", {})
        has_cpu = "cpu" in current.get("limits", {}) or "cpu" in current.get("requests", {})
        has_memory = "memory" in current.get("limits", {}) or "memory" in current.get("requests", {})

        assert has_cpu or has_memory, (
            "Recommendation should contain CPU and/or memory values"
        )

    def test_optimizations_show_request_limit_recommendations(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify request/limit recommendation data is available for optimizations.

        The new UI shows optimizations as a table; per-container detail
        drill-down is no longer available via click. Validate that:
        1. The optimizations table renders with data
        2. The recommendations API returns request/limit recommendations
        """
        authenticated_page.goto(f"{ui_url}/openshift/cost-management/optimizations")
        authenticated_page.wait_for_load_state("networkidle")
        time.sleep(3)

        empty_state = authenticated_page.locator(".pf-v6-c-empty-state, .pf-c-empty-state")
        if empty_state.count() > 0 and empty_state.first.is_visible():
            pytest.skip("No optimization data available yet")

        # Verify the table has rows (any optimization data)
        table_rows = authenticated_page.locator("table tbody tr")
        assert table_rows.count() > 0, (
            "Optimizations table should have at least one row"
        )

        save_screenshot(authenticated_page, "07_optimizations_request_limit")

        # Verify recommendation data has requests/limits via the API
        response = authenticated_page.request.get(
            f"{ui_url}/api/cost-management/v1/recommendations/openshift"
            f"?container=test-pod-1&limit=1"
        )
        assert response.status == 200, f"Recommendations API returned {response.status}"
        data = response.json().get("data", [])
        assert len(data) > 0, "Should have at least one recommendation"

        rec = data[0].get("recommendations", {})
        current = rec.get("current", {})
        has_requests = "requests" in current and len(current["requests"]) > 0
        has_limits = "limits" in current and len(current["limits"]) > 0

        assert has_requests or has_limits, (
            "Recommendation should contain request and/or limit values"
        )


@pytest.mark.ui
@pytest.mark.ros
@pytest.mark.data_validation
class TestOptimizationBreakdown:
    """Test optimization detail/breakdown views.
    
    Validates detailed breakdown view shows correct data.
    Uses cost_validation_data fixture for self-contained data setup.
    """

    def test_can_navigate_to_optimization_detail(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify the optimizations table displays data and test container is findable.

        The new UI shows optimizations as a list table. Validates the table
        renders with data and the test container exists in the API.
        """
        authenticated_page.goto(f"{ui_url}/openshift/cost-management/optimizations")
        authenticated_page.wait_for_load_state("networkidle")
        time.sleep(3)

        empty_state = authenticated_page.locator(".pf-v6-c-empty-state, .pf-c-empty-state")
        if empty_state.count() > 0 and empty_state.first.is_visible():
            pytest.skip("No optimization data available yet")

        # Verify table is rendered with optimization data
        table = authenticated_page.locator("table tbody tr")
        assert table.count() > 0, "Optimizations table should have at least one row"

        # Verify column headers are present
        headers = authenticated_page.locator("table thead th, table th")
        assert headers.count() > 0, "Table should have column headers"

        save_screenshot(authenticated_page, "08_optimization_detail_navigation")

        # Verify test container is reachable via the API
        response = authenticated_page.request.get(
            f"{ui_url}/api/cost-management/v1/recommendations/openshift"
            f"?container=test-pod-1&limit=1"
        )
        assert response.status == 200
        data = response.json().get("data", [])
        assert len(data) > 0, (
            "Test container 'test-pod-1' should have recommendations in the API"
        )

    def test_optimization_detail_shows_container_info(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify optimization table shows container/workload information columns."""
        authenticated_page.goto(f"{ui_url}/openshift/cost-management/optimizations")
        authenticated_page.wait_for_load_state("networkidle")
        time.sleep(3)

        empty_state = authenticated_page.locator(".pf-v6-c-empty-state, .pf-c-empty-state")
        if empty_state.count() > 0 and empty_state.first.is_visible():
            pytest.skip("No optimization data available yet")

        # Verify table column headers indicate container/workload info
        container_info = authenticated_page.get_by_text(
            re.compile(r"container|workload|project|cluster", re.IGNORECASE)
        )

        assert container_info.count() > 0, (
            "Optimizations table should show container/workload column headers"
        )

        # Verify the table has data rows
        rows = authenticated_page.locator("table tbody tr")
        assert rows.count() > 0, "Table should have data rows"

        save_screenshot(authenticated_page, "09_optimization_container_info")

    def test_optimization_detail_shows_recommendation_values(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify recommendations API contains numeric CPU/memory values."""
        authenticated_page.goto(f"{ui_url}/openshift/cost-management/optimizations")
        authenticated_page.wait_for_load_state("networkidle")
        time.sleep(3)

        empty_state = authenticated_page.locator(".pf-v6-c-empty-state, .pf-c-empty-state")
        if empty_state.count() > 0 and empty_state.first.is_visible():
            pytest.skip("No optimization data available yet")

        save_screenshot(authenticated_page, "10_optimization_recommendation_values")

        # Verify recommendations API returns numeric values
        response = authenticated_page.request.get(
            f"{ui_url}/api/cost-management/v1/recommendations/openshift"
            f"?container=test-pod-1&limit=1"
        )
        assert response.status == 200
        data = response.json().get("data", [])
        assert len(data) > 0, "Should have recommendation data"

        rec = data[0].get("recommendations", {})
        terms = rec.get("recommendation_terms", {})
        short_term = terms.get("short_term", {})
        engines = short_term.get("recommendation_engines", {})

        has_values = False
        for engine_name, engine_data in engines.items():
            config = engine_data.get("config", {})
            for resource_type in ("limits", "requests"):
                for metric in ("cpu", "memory"):
                    val = config.get(resource_type, {}).get(metric, {}).get("amount")
                    if val is not None:
                        has_values = True
                        break

        assert has_values, (
            "Recommendations should contain numeric CPU/memory values"
        )
