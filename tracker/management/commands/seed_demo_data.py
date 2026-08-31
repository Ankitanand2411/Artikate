"""Populate a fresh database with demo data.

Safely re-runnable: assets and employees are get_or_create'd on their
natural keys; check-outs and notices belonging to the demo assets are
wiped and rebuilt each run so counts stay deterministic. Also creates a
'demo' API user and prints its auth token, since every endpoint except
/health/ requires authentication.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from rest_framework.authtoken.models import Token

from tracker.models import Asset, CheckOut, Employee, OverdueNotice

ASSETS = [
    ("CAM-001", "Canon EOS R5", Asset.Category.CAMERA),
    ("CAM-002", "Sony FX3", Asset.Category.CAMERA),
    ("LAP-001", "ThinkPad X1 Carbon", Asset.Category.LAPTOP),
    ("LAP-002", "MacBook Pro 14", Asset.Category.LAPTOP),
    ("SEN-001", "Trimble R12 GNSS", Asset.Category.SENSOR),
    ("SEN-002", "FLIR Thermal Sensor", Asset.Category.SENSOR),
    ("VEH-001", "Tata Ace Field Van", Asset.Category.VEHICLE),
    ("VEH-002", "Mahindra Bolero Pickup", Asset.Category.VEHICLE),
]

EMPLOYEES = [
    ("EMP-001", "Asha Verma", "asha.verma@example.com", True),
    ("EMP-002", "Rohan Gupta", "rohan.gupta@example.com", True),
    ("EMP-003", "Meera Iyer", "meera.iyer@example.com", True),
    ("EMP-004", "Kabir Shah", "kabir.shah@example.com", False),  # inactive
]


class Command(BaseCommand):
    help = "Seed the database with demo assets, employees and check-outs."

    @transaction.atomic
    def handle(self, *args, **options):
        now = timezone.now()

        assets = {}
        for tag, name, category in ASSETS:
            asset, _ = Asset.objects.get_or_create(
                asset_tag=tag,
                defaults={
                    "name": name,
                    "category": category,
                    "purchase_date": now.date() - timedelta(days=365),
                },
            )
            assets[tag] = asset

        employees = {}
        for code, name, email, active in EMPLOYEES:
            emp, _ = Employee.objects.get_or_create(
                employee_code=code,
                defaults={"full_name": name, "email": email, "is_active": active},
            )
            employees[code] = emp

        # Rebuild demo check-outs from scratch so re-running stays exact.
        OverdueNotice.objects.filter(
            checkout__asset__in=assets.values()
        ).delete()
        CheckOut.objects.filter(asset__in=assets.values()).delete()
        Asset.objects.filter(pk__in=[a.pk for a in assets.values()]).update(
            status=Asset.Status.AVAILABLE
        )

        def make_checkout(asset_tag, emp_code, out_days_ago, due_in_days, returned_days_ago=None):
            """checked_out_at is auto_now_add, so it is backdated with a
            queryset .update() after creation."""
            co = CheckOut.objects.create(
                asset=assets[asset_tag],
                employee=employees[emp_code],
                due_at=now + timedelta(days=due_in_days),
            )
            CheckOut.objects.filter(pk=co.pk).update(
                checked_out_at=now - timedelta(days=out_days_ago)
            )
            if returned_days_ago is not None:
                CheckOut.objects.filter(pk=co.pk).update(
                    returned_at=now - timedelta(days=returned_days_ago)
                )
            else:
                Asset.objects.filter(pk=assets[asset_tag].pk).update(
                    status=Asset.Status.CHECKED_OUT
                )
            return co

        # Two currently overdue (open, due in the past)
        make_checkout("CAM-001", "EMP-001", out_days_ago=10, due_in_days=-4)
        make_checkout("LAP-001", "EMP-002", out_days_ago=8, due_in_days=-1)
        # Two returned on time (returned before due)
        make_checkout("SEN-001", "EMP-001", out_days_ago=14, due_in_days=-5, returned_days_ago=7)
        make_checkout("VEH-001", "EMP-003", out_days_ago=12, due_in_days=-6, returned_days_ago=8)
        # One returned late (returned after due)
        make_checkout("CAM-002", "EMP-002", out_days_ago=15, due_in_days=-9, returned_days_ago=2)
        # One open, not yet overdue
        make_checkout("LAP-002", "EMP-003", out_days_ago=2, due_in_days=5)

        user, created = get_user_model().objects.get_or_create(
            username="demo", defaults={"is_staff": True}
        )
        if created:
            user.set_password("demo-password")
            user.save()
        token, _ = Token.objects.get_or_create(user=user)

        self.stdout.write(self.style.SUCCESS("Seed complete."))
        self.stdout.write(f"Assets: {Asset.objects.count()}")
        self.stdout.write(f"Employees: {Employee.objects.count()}")
        self.stdout.write(f"CheckOuts: {CheckOut.objects.count()}")
        self.stdout.write(f"API user: demo / demo-password")
        self.stdout.write(f"API token: {token.key}")
