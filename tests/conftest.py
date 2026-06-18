import sys
from pathlib import Path

# Project root on path first, so the local `scripts` package wins over site-packages.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from scripts.budget import cache, telemetry_store
from scripts.feedback import store as feedback_store
from scripts.graphql_server import db
from scripts.profile import store as profile_store


@pytest.fixture()
def fresh_db(tmp_path):
    """A clean SQLite DB seeded from seed_data, isolated per test."""
    db.configure(str(tmp_path / "test_marketplace.db"))
    db.init_db(force=True)
    yield


@pytest.fixture()
def fresh_profile_db(tmp_path):
    """A clean profiles/history DB (seeded demo profiles), isolated per test."""
    profile_store.configure(str(tmp_path / "test_profiles.db"))
    profile_store.init_db(force=True)
    yield


@pytest.fixture(autouse=True)
def fresh_feedback_db(tmp_path):
    """A clean feedback DB, isolated per test. Autouse because the graph's finalize node captures
    feedback for any high-stakes turn — this keeps that write isolated instead of hitting the real
    feedback.db on disk during unrelated tests."""
    feedback_store.configure(str(tmp_path / "test_feedback.db"))
    feedback_store.init_db(force=True)
    yield


@pytest.fixture(autouse=True)
def fresh_telemetry_db(tmp_path):
    """A clean telemetry DB, isolated per test. Autouse because finalize persists telemetry for
    every turn — keeps that write off the real telemetry.db during unrelated tests."""
    telemetry_store.configure(str(tmp_path / "test_telemetry.db"))
    telemetry_store.init_db(force=True)
    yield


@pytest.fixture(autouse=True)
def fresh_cache():
    """The semantic cache is an in-memory module global — clear it between tests."""
    cache.clear()
    yield
    cache.clear()
