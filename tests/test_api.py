import os

# Set this before importing app.main so tests use an isolated database.
os.environ["MONGO_DB"] = "attendance_test_db"

from fastapi.testclient import TestClient

from app.main import app, client, db

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

def setup_module():
    # Ensure a clean database for this test module only.
    db.client.drop_database("attendance_test_db")


def teardown_module():
    # Remove test data after the module finishes.
    db.client.drop_database("attendance_test_db")
    client.close()


def test_health_returns_ok():
    with TestClient(app) as test_client:
        response = test_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_employee_and_reject_duplicate():
    with TestClient(app) as test_client:
        employee = {
            "emp_code": "EMP9001",
            "name": "API Test Employee",
            "email": "api9001@example.com",
            "department": "Testing",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }

        first = test_client.post("/employees", json=employee)
        duplicate = test_client.post("/employees", json=employee)

    assert first.status_code == 201
    assert first.json()["emp_code"] == "EMP9001"
    assert duplicate.status_code == 409


def test_reject_invalid_employee_code():
    with TestClient(app) as test_client:
        response = test_client.post(
            "/employees",
            json={
                "emp_code": "TEST9002",
                "name": "Invalid Code",
                "email": "invalid@example.com",
                "department": "Testing",
                "joined_on": "2026-01-01",
            },
        )

    assert response.status_code == 422


def test_punch_in_duplicate_is_rejected():
    with TestClient(app) as test_client:
        test_client.post(
            "/employees",
            json={
                "emp_code": "EMP9003",
                "name": "Punch Test Employee",
                "email": "punch9003@example.com",
                "department": "Testing",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            },
        )

        punch = {
            "emp_code": "EMP9003",
            "punched_at": 1791951060000,
            "status": "PRESENT",
        }

        first = test_client.post("/attendance/punch-in", json=punch)
        duplicate = test_client.post("/attendance/punch-in", json=punch)

    assert first.status_code == 201
    assert duplicate.status_code == 409


def test_punch_out_calculates_work_hours():
    with TestClient(app) as test_client:
        test_client.post(
            "/employees",
            json={
                "emp_code": "EMP9004",
                "name": "Punch Out Employee",
                "email": "punch9004@example.com",
                "department": "Testing",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            },
        )

        test_client.post(
            "/attendance/punch-in",
            json={
                "emp_code": "EMP9004",
                "punched_at": 1791951000000,
            },
        )

        response = test_client.post(
            "/attendance/punch-out",
            json={
                "emp_code": "EMP9004",
                "punched_at": 1791982800000,
            },
        )

    assert response.status_code == 200
    assert response.json()["punch_out"] == 1791982800000
    assert response.json()["work_hours"] == 8.83
    assert response.json()["half_day"] is False


def test_attendance_list_supports_pagination():
    with TestClient(app) as test_client:
        response = test_client.get(
            "/attendance",
            params={"page": 1, "page_size": 20},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["page"] == 1
    assert data["page_size"] == 20
    assert isinstance(data["items"], list)
    assert isinstance(data["total"], int)

def test_concurrent_punch_ins_create_exactly_one_record():
    with TestClient(app) as test_client:
        employee_response = test_client.post(
            "/employees",
            json={
                "emp_code": "EMP9005",
                "name": "Concurrency Test Employee",
                "email": "concurrency9005@example.com",
                "department": "Testing",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            },
        )
        assert employee_response.status_code == 201

        punch_payload = {
            "emp_code": "EMP9005",
            "punched_at": 1791951060000,
            "status": "PRESENT",
        }

        worker_count = 8
        barrier = Barrier(worker_count)

        def send_punch_in():
            barrier.wait(timeout=10)
            return test_client.post(
                "/attendance/punch-in",
                json=punch_payload,
            )

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            responses = list(
                executor.map(
                    lambda _: send_punch_in(),
                    range(worker_count),
                )
            )

        status_codes = [response.status_code for response in responses]

        assert status_codes.count(201) == 1
        assert status_codes.count(409) == worker_count - 1

        attendance_response = test_client.get(
            "/attendance",
            params={
                "emp_code": "EMP9005",
                "date_from": "2026-10-14",
                "date_to": "2026-10-14",
            },
        )

        assert attendance_response.status_code == 200
        assert attendance_response.json()["total"] == 1

def test_concurrent_punch_outs_only_one_succeeds():
    with TestClient(app) as test_client:
        employee_response = test_client.post(
            "/employees",
            json={
                "emp_code": "EMP9006",
                "name": "Concurrent Punch Out",
                "email": "concurrent9006@example.com",
                "department": "Testing",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            },
        )
        assert employee_response.status_code == 201

        punch_in_response = test_client.post(
            "/attendance/punch-in",
            json={
                "emp_code": "EMP9006",
                "punched_at": 1791951000000,
            },
        )
        assert punch_in_response.status_code == 201

        worker_count = 8
        barrier = Barrier(worker_count)

        def send_punch_out():
            barrier.wait(timeout=10)
            return test_client.post(
                "/attendance/punch-out",
                json={
                    "emp_code": "EMP9006",
                    "punched_at": 1791982800000,
                },
            )

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            responses = list(
                executor.map(
                    lambda _: send_punch_out(),
                    range(worker_count),
                )
            )

        status_codes = [response.status_code for response in responses]

        assert status_codes.count(200) == 1
        assert status_codes.count(409) == worker_count - 1

        attendance_response = test_client.get(
            "/attendance",
            params={
                "emp_code": "EMP9006",
                "date_from": "2026-10-14",
                "date_to": "2026-10-14",
            },
        )

        assert attendance_response.status_code == 200
        data = attendance_response.json()
        assert data["total"] == 1
        assert data["items"][0]["punch_out"] == 1791982800000
        
def test_concurrent_regularizations_preserve_history():
    with TestClient(app) as test_client:
        employee_response = test_client.post(
            "/employees",
            json={
                "emp_code": "EMP9007",
                "name": "Regularization Test",
                "email": "regularization9007@example.com",
                "department": "Testing",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            },
        )
        assert employee_response.status_code == 201

        punch_response = test_client.post(
            "/attendance/punch-in",
            json={
                "emp_code": "EMP9007",
                "punched_at": 1791951000000,
                "status": "PRESENT",
            },
        )
        assert punch_response.status_code == 201

        requests = [
            {
                "status": "WFH",
                "reason": "Concurrent update A",
                "regularized_by": "ADMIN_A",
            },
            {
                "status": "ON_DUTY",
                "reason": "Concurrent update B",
                "regularized_by": "ADMIN_B",
            },
        ]

        barrier = Barrier(len(requests))

        def send_regularization(payload):
            barrier.wait(timeout=10)
            return test_client.patch(
                "/attendance/EMP9007/2026-10-14",
                json=payload,
            )

        with ThreadPoolExecutor(max_workers=len(requests)) as executor:
            responses = list(executor.map(send_regularization, requests))

        status_codes = [response.status_code for response in responses]

        # One request wins; the stale concurrent update gets a conflict.
        assert status_codes.count(200) == 1
        assert status_codes.count(409) == 1

        attendance_response = test_client.get(
            "/attendance",
            params={
                "emp_code": "EMP9007",
                "date_from": "2026-10-14",
                "date_to": "2026-10-14",
            },
        )

        assert attendance_response.status_code == 200
        data = attendance_response.json()
        assert data["total"] == 1

        record = data["items"][0]
        assert record["status"] in ("WFH", "ON_DUTY")
        assert len(record["history"]) == 1
        assert record["history"][0]["by"] in ("ADMIN_A", "ADMIN_B")