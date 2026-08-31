from django.contrib import admin

from .models import Asset, CheckOut, Employee, OverdueNotice

admin.site.register(Asset)
admin.site.register(Employee)
admin.site.register(CheckOut)
admin.site.register(OverdueNotice)
