"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from typing import Annotated, Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required indexes idempotently at startup."""
    # employees collection
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1), ("department", 1)])

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def to_epoch_millis(dt: Optional[datetime]) -> Optional[int]:
    """Convert a UTC datetime to integer epoch milliseconds truncated to whole seconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_millis(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC datetime truncated to whole seconds (R1)."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def round_half_up(val: Any, decimals: int) -> float:
    """Round a number half-up to the given number of decimal places (R8)."""
    d = Decimal(str(val))
    q = Decimal("10") ** (-decimals)
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def get_attendance_date_and_shifts(punch_in_utc: datetime, shift_start: str, shift_end: str):
    """
    R1: Attendance date and shift boundaries in IST.
    For overnight shifts (shift_end <= shift_start), a punch-in earlier than shift_end
    belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start

    h_start, m_start = map(int, shift_start.split(":"))
    h_end, m_end = map(int, shift_end.split(":"))
    shift_end_time = time(h_end, m_end)

    if is_overnight and punch_in_ist.time() < shift_end_time:
        att_date = punch_in_ist.date() - timedelta(days=1)
    else:
        att_date = punch_in_ist.date()

    date_str = att_date.isoformat()

    start_dt = datetime.combine(att_date, time(h_start, m_start), tzinfo=IST)
    if is_overnight:
        end_date = att_date + timedelta(days=1)
    else:
        end_date = att_date
    end_dt = datetime.combine(end_date, time(h_end, m_end), tzinfo=IST)

    return date_str, start_dt, end_dt


def compute_late_minutes(punch_in_utc: datetime, shift_start_dt: datetime) -> int:
    """
    R2: Grace period 10 minutes (600s). Late only if strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start, not since grace period ended.
    """
    delta_sec = (punch_in_utc - shift_start_dt).total_seconds()
    if delta_sec > 600:
        return int(delta_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    delta_sec = int((punch_out_utc - punch_in_utc).total_seconds())
    val = Decimal(delta_sec) / Decimal(3600)
    return float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_utc: datetime, shift_end_dt: datetime) -> int:
    """R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0."""
    delta_sec = (punch_out_utc - shift_end_dt).total_seconds()
    minutes = int(delta_sec // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: After punch-out, half_day is true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(joined_on_str: str, month_str: str) -> int:
    """
    R7: Count Mon-Fri days in month from joined_on.
    """
    y, m = map(int, month_str.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    first_date = date(y, m, 1)
    last_date = date(y, m, num_days)

    joined_date = date.fromisoformat(joined_on_str)
    if joined_date > last_date:
        return 0

    start_date = max(first_date, joined_date)
    working_days = 0
    cur = start_date
    while cur <= last_date:
        if cur.weekday() < 5:  # Mon=0, Fri=4
            working_days += 1
        cur += timedelta(days=1)
    return working_days


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #
def serialize_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at = to_epoch_millis(created_at)
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_at,
    }


def serialize_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    history_entries = []
    for h in doc.get("history", []) or []:
        raw_changes = h.get("changes", {}) or {}
        changes_dict = {}
        for k, v in raw_changes.items():
            f_val = v.get("from")
            t_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                f_val = to_epoch_millis(f_val) if isinstance(f_val, datetime) else f_val
                t_val = to_epoch_millis(t_val) if isinstance(t_val, datetime) else t_val
            changes_dict[k] = {"from": f_val, "to": t_val}

        at_val = h.get("at")
        if isinstance(at_val, datetime):
            at_val = to_epoch_millis(at_val)

        history_entries.append({
            "at": at_val,
            "by": h.get("by", ""),
            "reason": h.get("reason", ""),
            "changes": changes_dict,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc.get("status", "PRESENT"),
        "punch_in": to_epoch_millis(doc.get("punch_in")),
        "punch_out": to_epoch_millis(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_entries,
    }


# --------------------------------------------------------------------------- #
# Request Models
# --------------------------------------------------------------------------- #
EpochMillisType = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillisType] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillisType] = None


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillisType] = None
    punch_out: Optional[EpochMillisType] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    model_config = ConfigDict(extra="ignore")


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MongoDB unavailable",
        )


@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="emp_code already exists",
        )
    return serialize_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        ts = from_epoch_millis(body.punched_at)
    else:
        now_dt = datetime.now(timezone.utc)
        ts = from_epoch_millis(int(now_dt.timestamp() * 1000))

    att_date, start_dt, _ = get_attendance_date_and_shifts(
        ts, emp["shift_start"], emp["shift_end"]
    )
    late_min = compute_late_minutes(ts, start_dt)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already punched in for this date",
        )

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        out_dt = from_epoch_millis(body.punched_at)
    else:
        now_dt = datetime.now(timezone.utc)
        out_dt = from_epoch_millis(int(now_dt.timestamp() * 1000))

    out_ms = to_epoch_millis(out_dt)

    # Find the most recent record whose punch_in <= punched_at
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": out_dt}},
        sort=[("punch_in", -1)],
    )

    if not candidate:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No punch-in found",
        )

    if candidate.get("punch_out") is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record already punched out",
        )

    in_dt = candidate["punch_in"]
    in_ms = to_epoch_millis(in_dt)

    # 422 if punched_at <= punch_in or more than 24 hours after it
    if out_ms <= in_ms or (out_ms - in_ms) > 24 * 3600 * 1000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at must be after punch_in and within 24 hours",
        )

    # Compute derived fields
    work_hrs = compute_work_hours(in_dt, out_dt)
    half = compute_half_day(work_hrs)

    _, _, end_dt = get_attendance_date_and_shifts(
        in_dt, emp["shift_start"], emp["shift_end"]
    )
    ot = compute_overtime(out_dt, end_dt)

    # Race-safe atomic update
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot,
                "half_day": half,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record already punched out",
        )

    return serialize_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from must be <= date_to",
        )

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [serialize_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No attendance record found for this date",
        )

    curr_status = record.get("status")
    curr_in_dt = record.get("punch_in")
    curr_out_dt = record.get("punch_out")
    curr_work_hours = record.get("work_hours")
    curr_late_minutes = record.get("late_minutes", 0)
    curr_overtime_minutes = record.get("overtime_minutes", 0)
    curr_half_day = record.get("half_day", False)
    curr_history = record.get("history", []) or []

    new_status = body.status if body.status is not None else curr_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Cannot provide punch_in or punch_out when status is ABSENT or LEAVE",
            )
        new_in_dt = None
        new_out_dt = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        if body.punch_in is not None:
            new_in_dt = from_epoch_millis(body.punch_in)
        else:
            new_in_dt = curr_in_dt

        if new_in_dt is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires punch_in",
            )

        calc_date, start_dt, end_dt = get_attendance_date_and_shifts(
            new_in_dt, emp["shift_start"], emp["shift_end"]
        )
        if calc_date != date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"punch_in belongs to attendance date {calc_date}, not {date}",
            )

        if body.punch_out is not None:
            new_out_dt = from_epoch_millis(body.punch_out)
        else:
            new_out_dt = curr_out_dt

        if new_out_dt is not None:
            if new_out_dt <= new_in_dt:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be after punch_in",
                )
            if (new_out_dt - new_in_dt).total_seconds() > 24 * 3600:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be within 24 hours of punch_in",
                )
            new_work_hours = compute_work_hours(new_in_dt, new_out_dt)
            new_half_day = compute_half_day(new_work_hours)
            new_overtime_minutes = compute_overtime(new_out_dt, end_dt)
        else:
            new_work_hours = None
            new_half_day = False
            new_overtime_minutes = 0

        new_late_minutes = compute_late_minutes(new_in_dt, start_dt)

    # Track actual changes
    mongo_changes = {}
    if curr_status != new_status:
        mongo_changes["status"] = {"from": curr_status, "to": new_status}
    if curr_in_dt != new_in_dt:
        mongo_changes["punch_in"] = {"from": curr_in_dt, "to": new_in_dt}
    if curr_out_dt != new_out_dt:
        mongo_changes["punch_out"] = {"from": curr_out_dt, "to": new_out_dt}
    if curr_work_hours != new_work_hours:
        mongo_changes["work_hours"] = {"from": curr_work_hours, "to": new_work_hours}
    if curr_late_minutes != new_late_minutes:
        mongo_changes["late_minutes"] = {"from": curr_late_minutes, "to": new_late_minutes}
    if curr_overtime_minutes != new_overtime_minutes:
        mongo_changes["overtime_minutes"] = {
            "from": curr_overtime_minutes,
            "to": new_overtime_minutes,
        }
    if curr_half_day != new_half_day:
        mongo_changes["half_day"] = {"from": curr_half_day, "to": new_half_day}

    if not mongo_changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request changes nothing",
        )

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": mongo_changes,
    }

    # Optimistic locking against concurrent regularizations
    filter_q = {
        "_id": record["_id"],
        f"history.{len(curr_history)}": {"$exists": False},
    }
    update_q = {
        "$set": {
            "status": new_status,
            "punch_in": new_in_dt,
            "punch_out": new_out_dt,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime_minutes,
            "half_day": new_half_day,
        },
        "$push": {"history": history_entry},
    }

    updated = db.attendance_logs.find_one_and_update(
        filter_q, update_q, return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification conflict",
        )

    return serialize_attendance_doc(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    start_str = f"{month}-01"
    end_str = f"{month}-{num_days:02d}"

    working_days = count_working_days(emp["joined_on"], month)

    pipe = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_str, "$lte": end_str}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$lte": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {"dateString": "$date"}
                                                }
                                            },
                                            5,
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipe))
    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        att_pct = round_half_up(
            Decimal(str(present_days)) / Decimal(str(working_days)) * Decimal(100), 2
        )
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None):
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    match_emp: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$lte": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {"dateString": "$logs.date"}
                                                }
                                            },
                                            5,
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "valid_work_hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "LEAVE"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipe = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipe))

    items = []
    for doc in results:
        headcount = len(doc.get("employees", []))
        if headcount == 0:
            continue

        wh_list = doc.get("valid_work_hours", [])
        if wh_list:
            wh_sum = sum(Decimal(str(x)) for x in wh_list)
            avg_wh = round_half_up(wh_sum / Decimal(len(wh_list)), 2)
        else:
            avg_wh = None

        items.append({
            "department": doc["_id"],
            "headcount": headcount,
            "present_days": float(doc.get("present_days", 0.0)),
            "avg_work_hours": avg_wh,
            "late_count": int(doc.get("late_count", 0)),
            "total_late_minutes": int(doc.get("total_late_minutes", 0)),
            "leave_count": int(doc.get("leave_count", 0)),
            "on_duty_count": int(doc.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
):
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    stages: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        stages.append({"$match": {"emp.department": department}})

    stages.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return stages


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipe = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipe))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_str: str, to_str: str):
    d_from = datetime.fromisoformat(from_str)
    d_to = datetime.fromisoformat(to_str)
    d_bound_end = d_to + timedelta(days=1)

    emp_codes = [
        e["emp_code"]
        for e in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})
    ]

    return [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_str, "$lte": to_str},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "d": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "d",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [d_from, d_bound_end],
                },
            }
        },
        {
            "$project": {
                "d": 1,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    dep_exists = db.employees.count_documents({"department": department})
    if dep_exists == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)

    if d_to < d_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'to' date must be >= 'from' date",
        )

    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Date range exceeds 92 days",
        )

    pipe = build_department_trend_pipeline(department, from_date, to_date)
    raw_items = list(db.attendance_logs.aggregate(pipe))

    items = []
    for row in raw_items:
        ar = row.get("attendance_rate")
        m7 = row.get("moving_avg_7d")
        items.append({
            "date": row["date"],
            "is_working_day": row["is_working_day"],
            "headcount": int(row["headcount"]),
            "present_count": float(row["present_count"]),
            "late_count": int(row["late_count"]),
            "attendance_rate": round_half_up(ar, 4) if ar is not None else None,
            "moving_avg_7d": round_half_up(m7, 4) if m7 is not None else None,
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: Optional[int] = Query(1, ge=1),
    page_size: Optional[int] = Query(20, ge=1, le=100),
):
    valid_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint not in valid_endpoints:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown endpoint: {endpoint}",
        )

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="date_from must be <= date_to",
            )
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status_filter:
            q["status"] = status_filter

        skip = ((page or 1) - 1) * (page_size or 20)
        explain_res = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size or 20,
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="emp_code and month are required",
            )
        y, m = map(int, month.split("-"))
        num_days = calendar.monthrange(y, m)[1]
        pipe = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": f"{month}-01", "$lte": f"{month}-{num_days:02d}"},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {
                                            "$lte": [
                                                {
                                                    "$isoDayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$date"
                                                        }
                                                    }
                                                },
                                                5,
                                            ]
                                        },
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                }
            },
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required",
            )
        pipe = build_department_summary_pipeline(month, department)
        explain_res = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_res,
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required",
            )
        pipe = build_late_leaderboard_pipeline(month, limit or 10, department)
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="department, from, and to are required",
            )
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", from_date) or not re.match(
            r"^\d{4}-\d{2}-\d{2}$", to_date
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid date format",
            )
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if d_to < d_from:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="'to' date must be >= 'from' date",
            )
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Date range exceeds 92 days",
            )
        pipe = build_department_trend_pipeline(department, from_date, to_date)
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }
