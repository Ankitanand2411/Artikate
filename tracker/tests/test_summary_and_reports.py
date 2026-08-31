from datetime import timedelta

import pytest
from django.utils import timezone

from tracker.models import Asset
from .factories import make_asset, make_checkout, make_employee

pytestmark = pytest.mark.django_db


def test_employee_summary_four_numbers(client):
    emp = make_employee()
    # Two returned: held 2 days and 4 days -> mean 3.0
    make_checkout(employee=emp, checked_out_days_ago=10, due_in_days=1,
                  returned_days_ago=8)
    make_checkout(employee=emp, checked_out_days_ago=10, due_in_days=1,
                  returned_days_ago=6)
    # One open and overdue
    make_checkout(employee=emp, checked_out_days_ago=5, due_in_days=-2)
    # One open, not overdue
    make_checkout(employee=emp, checked_out_days_ago=1, due_in_days=5)

    resp = client.get(f"/api/v1/employees/{emp.employee_code}/summary/")
    assert resp.status_code == 200
    assert resp.data["lifetime_checkouts"] == 4
    assert resp.data["currently_held"] == 2
    assert resp.data["currently_overdue"] == 1
    assert resp.data["mean_hold_days"] == pytest.approx(3.0, abs=0.01)


def test_summary_unknown_employee_404(client):
    assert client.get("/api/v1/employees/GHOST/summary/").status_code == 404


def test_summary_no_returns_mean_is_null(client):
    emp = make_employee()
    make_checkout(employee=emp, due_in_days=5)
    resp = client.get(f"/api/v1/employees/{emp.employee_code}/summary/")
    assert resp.data["mean_hold_days"] is None


def test_overdue_report_content_and_order(client):
    worst = make_checkout(checked_out_days_ago=10, due_in_days=-5)
    mild = make_checkout(checked_out_days_ago=10, due_in_days=-1)
    # Due essentially "now" (1 second ago): included, days_overdue == 0.
    just_due = make_checkout(checked_out_days_ago=1, due_in_days=0)
    just_due.due_at = timezone.now() - timedelta(seconds=1)
    just_due.save(update_fields=["due_at"])
    # Excluded: not yet due / already returned
    make_checkout(checked_out_days_ago=1, due_in_days=5)
    make_checkout(checked_out_days_ago=9, due_in_days=-3, returned_days_ago=1)

    resp = client.get("/api/v1/reports/overdue/")
    assert resp.status_code == 200
    rows = resp.data["results"]
    assert [r["asset_tag"] for r in rows] == [
        worst.asset.asset_tag,
        mild.asset.asset_tag,
        just_due.asset.asset_tag,
    ]
    assert rows[0]["days_overdue"] == 5
    assert rows[2]["days_overdue"] == 0
    assert rows[0]["employee_code"] == worst.employee.employee_code
    assert rows[0]["asset_name"] == worst.asset.name


def test_overdue_report_query_count(client, django_assert_num_queries):
    for _ in range(5):
        make_checkout(checked_out_days_ago=10, due_in_days=-2)
    # 1 pagination COUNT + 1 page query. No per-row queries.
    with django_assert_num_queries(2):
        resp = client.get("/api/v1/reports/overdue/")
    assert resp.data["count"] == 5


def test_asset_list_filter_and_search(client):
    make_asset(category=Asset.Category.CAMERA, name="Red Cinema Cam")
    make_asset(category=Asset.Category.LAPTOP, name="Gray Laptop")
    resp = client.get("/api/v1/assets/", {"category": "CAMERA"})
    assert resp.data["count"] == 1
    resp = client.get("/api/v1/assets/", {"search": "Cinema"})
    assert resp.data["count"] == 1
