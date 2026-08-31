from django.db import models


class Asset(models.Model):
    class Category(models.TextChoices):
        CAMERA = "CAMERA", "Camera"
        LAPTOP = "LAPTOP", "Laptop"
        SENSOR = "SENSOR", "Sensor"
        VEHICLE = "VEHICLE", "Vehicle"

    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        CHECKED_OUT = "CHECKED_OUT", "Checked out"
        MAINTENANCE = "MAINTENANCE", "Maintenance"

    # unique=True already creates a b-tree index in PostgreSQL.
    asset_tag = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=16, choices=Category.choices)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.AVAILABLE
    )
    purchase_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.asset_tag} ({self.name})"


class Employee(models.Model):
    employee_code = models.CharField(max_length=16, unique=True)
    full_name = models.CharField(max_length=120)
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.employee_code} ({self.full_name})"


class CheckOut(models.Model):
    asset = models.ForeignKey(
        Asset, on_delete=models.PROTECT, related_name="checkouts"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="checkouts"
    )
    checked_out_at = models.DateTimeField(auto_now_add=True)
    due_at = models.DateTimeField()
    returned_at = models.DateTimeField(null=True, blank=True)
    condition_note = models.TextField(blank=True)

    def __str__(self):
        return f"CheckOut #{self.pk} {self.asset_id} -> {self.employee_id}"


class OverdueNotice(models.Model):
    checkout = models.ForeignKey(
        CheckOut, on_delete=models.CASCADE, related_name="notices"
    )
    notice_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # This is what makes the Celery task idempotent at the database
            # level: a second run on the same day hits the constraint instead
            # of creating a duplicate notice.
            models.UniqueConstraint(
                fields=["checkout", "notice_date"],
                name="unique_notice_per_checkout_per_day",
            )
        ]

    def __str__(self):
        return f"Notice for checkout {self.checkout_id} on {self.notice_date}"
