"""Rule 7: two simultaneous check-outs of the same asset -- exactly one
succeeds. Real threads against a real PostgreSQL connection, which is why
this uses transaction=True (each thread needs its own committed view)."""
import threading
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from tracker.models import Asset, CheckOut
from .factories import make_asset, make_employee


@pytest.mark.django_db(transaction=True)
def test_concurrent_checkout_exactly_one_succeeds():
    asset = make_asset()
    emp_a = make_employee()
    emp_b = make_employee()
    user = get_user_model().objects.create_user("racer", password="pw")

    barrier = threading.Barrier(2)
    results = []

    def attempt(employee_code):
        try:
            barrier.wait(timeout=10)
            client = APIClient()
            client.force_authenticate(user)
            resp = client.post(
                "/api/v1/checkouts/",
                {
                    "asset_tag": asset.asset_tag,
                    "employee_code": employee_code,
                    "due_at": (timezone.now() + timedelta(days=7)).isoformat(),
                },
            )
            results.append(resp.status_code)
        finally:
            # Each thread opened its own DB connection; close it or the
            # test database cannot be torn down.
            connection.close()

    threads = [
        threading.Thread(target=attempt, args=(emp_a.employee_code,)),
        threading.Thread(target=attempt, args=(emp_b.employee_code,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert sorted(results) == [201, 409], results
    assert CheckOut.objects.filter(asset=asset, returned_at__isnull=True).count() == 1
    asset.refresh_from_db()
    assert asset.status == Asset.Status.CHECKED_OUT
