import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def api_user(db):
    return get_user_model().objects.create_user("tester", password="pw")


@pytest.fixture
def client(api_user):
    c = APIClient()
    c.force_authenticate(api_user)
    return c
