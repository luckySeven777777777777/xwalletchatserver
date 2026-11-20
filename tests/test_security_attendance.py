from datetime import datetime, timezone, timedelta
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import (
    _normalize_break_type, _parse_attendance_event_time,
    _AUTH_FAILURES, _auth_bucket, _record_auth_failure, _check_auth_limit,
    _clear_auth_failures, _offline_alert_due, _validate_attendance_group_id,
)
from app.db import Base
from app.models import User, EmployeeAttendanceEvent, AttendanceIngestReceipt


def test_break_type_aliases_are_normalized():
    assert _normalize_break_type("Eat") == "Eat"
    assert _normalize_break_type("Toilet") == "Tolied"
    assert _normalize_break_type("Pee") == "Pee"
    with pytest.raises(HTTPException):
        _normalize_break_type("unknown")


def test_event_timestamp_rejects_far_future_and_old_values():
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(HTTPException):
        _parse_attendance_event_time((now + timedelta(minutes=10)).isoformat(), now)
    with pytest.raises(HTTPException):
        _parse_attendance_event_time((now - timedelta(days=8)).isoformat(), now)
    assert _parse_attendance_event_time((now - timedelta(minutes=1)).isoformat(), now) == now - timedelta(minutes=1)


def test_login_rate_limiter_blocks_after_five_failures():
    class Client:
        host = "127.0.0.1"
    class Request:
        client = Client()
    key = _auth_bucket(Request(), "test-admin")
    _clear_auth_failures(key)
    for _ in range(5):
        _record_auth_failure(key)
    with pytest.raises(HTTPException) as exc:
        _check_auth_limit(key)
    assert exc.value.status_code == 429
    _clear_auth_failures(key)


def test_attendance_records_and_receipts_survive_database_reopen(tmp_path):
    db_path = tmp_path / "persistent-test.db"
    url = f"sqlite:///{db_path}"
    engine1 = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine1)
    Session1 = sessionmaker(bind=engine1)
    with Session1() as db:
        user = User(username="attendance-test", password_hash="not-a-real-login-hash", role="employee")
        db.add(user)
        db.commit()
        db.refresh(user)
        db.add(EmployeeAttendanceEvent(user_id=user.id, break_type="Eat",
                                       started_at=datetime.now(timezone.utc)))
        db.add(AttendanceIngestReceipt(event_id="test-event-12345678", user_id=user.id, action="break"))
        db.commit()
    engine1.dispose()

    engine2 = create_engine(url, connect_args={"check_same_thread": False})
    Session2 = sessionmaker(bind=engine2)
    with Session2() as db:
        assert db.query(EmployeeAttendanceEvent).count() == 1
        assert db.query(AttendanceIngestReceipt).filter_by(event_id="test-event-12345678").count() == 1
    engine2.dispose()


def test_offline_alert_threshold_is_exactly_fifteen_minutes():
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    assert not _offline_alert_due(now - timedelta(seconds=899), now, 900)
    assert _offline_alert_due(now - timedelta(seconds=900), now, 900)


def test_attendance_group_id_must_match_exactly():
    _validate_attendance_group_id("-1001234567890", "-1001234567890")
    with pytest.raises(HTTPException) as exc:
        _validate_attendance_group_id("-1001234567890", "")
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        _validate_attendance_group_id("", "-1001234567890")
    assert exc.value.status_code == 503
