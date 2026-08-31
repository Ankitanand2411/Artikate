from datetime import timedelta

import pytest
from django.utils import timezone

from tracker.models import Asset, CheckOut
from .factories import make_asset, make_checkout, make_employee

pytestmark = pytest.mark.django_db


def _due(days=7):
    return (timezone.now() + timedelta(days=days)).isoformat()


def checkout_payload(asset, employee, due_days=7):
    return {
        "asset_tag": asset.asset_tag,
        "employee_code": employee.employee_code,
        "due_at": _due(due_days),
    }


def test_successful_checkout_creates_row_and_flips_status(client):
    asset, emp = make_asset(), make_employee()
    resp = client.post("/api/v1/checkouts/", checkout_payload(asset, emp))
    assert resp.status_code == 201
    asset.refresh_from_db()
    assert asset.status == Asset.Status.CHECKED_OUT
    assert CheckOut.objects.filter(asset=asset, returned_at__isnull=True).count() == 1


def test_unavailable_asset_returns_409(client):
    asset = make_asset(status=Asset.Status.MAINTENANCE)
    emp = make_employee()
    resp = client.post("/api/v1/checkouts/", checkout_payload(asset, emp))
    assert resp.status_code == 409


def test_inactive_employee_returns_400(client):
    asset = make_asset()
    emp = make_employee(is_active=False)
    resp = client.post("/api/v1/checkouts/", checkout_payload(asset, emp))
    assert resp.status_code == 400


def test_fourth_open_checkout_returns_409(client):
    emp = make_employee()
    for _ in range(3):
        resp = client.post(
            "/api/v1/checkouts/", checkout_payload(make_asset(), emp)
        )
        assert resp.status_code == 201
    resp = client.post("/api/v1/checkouts/", checkout_payload(make_asset(), emp))
    assert resp.status_code == 409


def test_due_at_in_past_returns_400(client):
    resp = client.post(
        "/api/v1/checkouts/",
        checkout_payload(make_asset(), make_employee(), due_days=-1),
    )
    assert resp.status_code == 400


def test_due_at_beyond_30_days_returns_400(client):
    resp = client.post(
        "/api/v1/checkouts/",
        checkout_payload(make_asset(), make_employee(), due_days=31),
    )
    assert resp.status_code == 400


def test_unknown_asset_and_employee_return_404(client):
    emp = make_employee()
    resp = client.post(
        "/api/v1/checkouts/",
        {"asset_tag": "NOPE", "employee_code": emp.employee_code, "due_at": _due()},
    )
    assert resp.status_code == 404
    asset = make_asset()
    resp = client.post(
        "/api/v1/checkouts/",
        {"asset_tag": asset.asset_tag, "employee_code": "NOPE", "due_at": _due()},
    )
    assert resp.status_code == 404


def test_return_flow_and_double_return_409(client):
    asset, emp = make_asset(), make_employee()
    checkout_id = client.post(
        "/api/v1/checkouts/", checkout_payload(asset, emp)
    ).data["id"]

    resp = client.post(
        f"/api/v1/checkouts/{checkout_id}/return/",
        {"condition_note": "fine", "needs_maintenance": False},
    )
    assert resp.status_code == 200
    asset.refresh_from_db()
    assert asset.status == Asset.Status.AVAILABLE

    resp = client.post(f"/api/v1/checkouts/{checkout_id}/return/", {})
    assert resp.status_code == 409


def test_return_with_maintenance_flag(client):
    asset, emp = make_asset(), make_employee()
    checkout_id = client.post(
        "/api/v1/checkouts/", checkout_payload(asset, emp)
    ).data["id"]
    resp = client.post(
        f"/api/v1/checkouts/{checkout_id}/return/",
        {"condition_note": "lens cracked", "needs_maintenance": True},
    )
    assert resp.status_code == 200
    asset.refresh_from_db()
    assert asset.status == Asset.Status.MAINTENANCE


def test_asset_detail_current_holder(client):
    asset, emp = make_asset(), make_employee()
    client.post("/api/v1/checkouts/", checkout_payload(asset, emp))
    resp = client.get(f"/api/v1/assets/{asset.id}/")
    assert resp.data["current_holder"] == {
        "employee_code": emp.employee_code,
        "full_name": emp.full_name,
    }


def test_health_is_public(db):
    from rest_framework.test import APIClient

    resp = APIClient().get("/api/v1/health/")
    assert resp.status_code == 200
    assert resp.data["database"] is True
