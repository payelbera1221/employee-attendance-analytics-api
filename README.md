# Employee Attendance & Analytics API

A FastAPI backend implementing attendance management, manual regularization, and analytics over MongoDB according to OpenAPI 2.0 specifications and business rules R1–R10.

## Requirements & Stack
- Python 3.11+
- MongoDB 6.0+ (tested on MongoDB 7.0 / 8.0)
- FastAPI, PyMongo, Pydantic v2, Uvicorn

## Setup & Running

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment Variables**:
   Set `MONGO_URI` and `MONGO_DB` in your environment (defaults to `mongodb://localhost:27017` and `attendance_db`).

3. **Seed sample data** (optional):
   ```bash
   python sample_seed.py
   ```

4. **Start the application**:
   ```bash
   uvicorn app.main:app --port 8000
   ```

All indexes are created idempotently at application startup via FastAPI's lifespan handler.

## Overview of Implementation
- **All application code** is located in `app/main.py`.
- **Review of starter defects** is documented in `REVIEW.md`.
- **Architectural decisions & query plans** are detailed in `DECISIONS.md`.
- **Explain plans** for all analytics queries are exposed via `GET /admin/explain/{endpoint}`, using index scans (`IXSCAN`) and zero collection scans (`COLLSCAN`).

## Running Tests

Install the project dependencies, including the test tools:

```bash
python -m pip install -r requirements.txt
```

Ensure MongoDB is running, then execute:

```bash
python -m pytest tests -v
```

The API tests use a separate `attendance_test_db` database and remove that test database before and after the test module. Do not configure `MONGO_DB` to point to a database containing data you need to preserve.

The test suite covers attendance calculations, employee validation, attendance endpoints, duplicate requests, and concurrent punch-in, punch-out, and regularization behavior.