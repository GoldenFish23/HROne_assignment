"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
import json
import logging
import calendar

from datetime import datetime, timedelta, timezone, date as date_type
from typing import Optional, Annotated, Literal
from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_HALF_UP


from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError
from bson import json_util

load_dotenv()

# creating logger for better logging
log = logging.getLogger("attendance")

# connection timeout + timezone awareness
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


# epoch from the mentioned datetime
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# patterns for validation matches
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(MONTH_PATTERN)
EMP_DEPT_JOINED_HINT = [("department", ASCENDING), ("joined_on", ASCENDING)]
# Fixed var assumpted.
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
WEEKDAYS = [2, 3, 4, 5, 6]
_indexes_ready = False

# check index existence and creates if not available.
def ensure_indexes():
    global _indexes_ready
    specs = [
        (db.employees, [("emp_code", ASCENDING)], {"unique": True}),
        (db.employees, [("department", ASCENDING), ("emp_code", ASCENDING)], {}),
        (db.employees, EMP_DEPT_JOINED_HINT, {}),
        (db.attendance_logs, [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True}),
        (db.attendance_logs, [("date", DESCENDING), ("emp_code", ASCENDING)], {}),
        (db.attendance_logs, [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {}),
        (db.attendance_logs, [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {}),
    ]
    ok = True
    for coll, keys, opts in specs:
        try:
            coll.create_index(keys, **opts)
        except OperationFailure as exc:
            log.warning("index %s on %s skipped: %s", keys, coll.name, exc)
        except PyMongoError as exc:
            log.warning("index creation failed: %s", exc)
            ok = False
    _indexes_ready = ok

# async function to check indexes
@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except Exception as exc:  # do not crash startup; health reports Mongo state
        log.warning("startup index creation failed: %s", exc)
    yield

# app with startup task
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
# unprocessable value error
def unprocessable(msg: str, loc=("body",)) -> HTTPException:
    return HTTPException(422, [{"type": "value_error", "loc": list(loc), "msg": msg, "input": None}])


def from_millis(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)


def to_millis(dt):
    if dt is None or isinstance(dt, int):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc_ms() -> datetime:
    n = datetime.now(timezone.utc)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)

# truncating function
def truncate_to_seconds(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).replace(microsecond=0)

# parsing hour & minute
def parse_hhmm(s: str):
    h, m = map(int, s.split(":"))
    return h, m

# attendance date from truncated seconds
def attendance_date(ts: datetime, shift_start: str, shift_end: str) -> str:
    ts = truncate_to_seconds(ts)
    d = ts.date()
    if shift_end <= shift_start and (ts.hour, ts.minute, ts.second) < (*parse_hhmm(shift_end), 0):
        d = d - timedelta(days=1)
    return d.isoformat()

# computing late minutes
def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    h, m = parse_hhmm(shift_start)
    start = datetime.fromisoformat(date_str).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    delta = int((truncate_to_seconds(punch_in) - start).total_seconds())
    return delta // 60 if delta > 600 else 0

# compute work hours
def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    diff_seconds = int((truncate_to_seconds(punch_out) - truncate_to_seconds(punch_in)).total_seconds())
    hours = (Decimal(diff_seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)

# computing overtime
def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: Optional[str] = None) -> int:
    h, m = parse_hhmm(shift_end)
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    if shift_start is not None and shift_end <= shift_start:
        end += timedelta(days=1)
    minutes = int((truncate_to_seconds(punch_out) - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0

# computing halfdays
def compute_halfday(work_hours: float) -> bool:
    return work_hours < 4.50

# computing round up with round half up technique
def round_half_up(value, places: int = 2) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(repr(round(float(value), 6))).quantize(q, rounding=ROUND_HALF_UP))


def ratio_dec(num, den, places: int) -> Decimal:
    q = Decimal(1).scaleb(-places)
    return (Decimal(repr(round(float(num), 6))) / Decimal(den)).quantize(q, rounding=ROUND_HALF_UP)

# parsing date
def parse_date_str(value: str, name: str) -> date_type:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise unprocessable(f"{name} must be YYYY-MM-DD", ("query", name))
    try:
        return date_type.fromisoformat(value)
    except ValueError:
        raise unprocessable(f"{name} is not a valid date", ("query", name))

# parsing month
def parse_month(month: str):
    if not isinstance(month, str) or not MONTH_RE.fullmatch(month):
        raise unprocessable("month must be YYYY-MM", ("query", "month"))
    y, m = int(month[:4]), int(month[5:])
    return date_type(y, m, 1), date_type(y, m, calendar.monthrange(y, m)[1])

# extracting joined date
def joined_date(emp: dict) -> date_type:
    j = emp.get("joined_on")
    if isinstance(j, datetime):
        return j.date()
    return date_type.fromisoformat(str(j)[:10])

# computing counts of working days
def count_working_days(start: date_type, end: date_type) -> int:
    if start > end:
        return 0
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)

# result employee data in proper format
def emp_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_millis(doc.get("created_at")),
    }

# result history in proper format
def history_out(h: dict) -> dict:
    changes = {}
    for k, v in (h.get("changes") or {}).items():
        changes[k] = {
            "from": to_millis(v.get("from")) if k in ("punch_in", "punch_out") else v.get("from"),
            "to": to_millis(v.get("to")) if k in ("punch_in", "punch_out") else v.get("to"),
        }
    return {"at": to_millis(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes}

# result records in proper format
def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_millis(doc.get("punch_in")),
        "punch_out": to_millis(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
# Validation for epoch
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
# marked status
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=TIME_PATTERN)
    shift_end: str = Field("18:30", pattern=TIME_PATTERN)
    joined_on: str
    
    # Validator code employeein data inputs
    @model_validator(mode="after")
    def _check(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        if not DATE_RE.fullmatch(self.joined_on):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date_type.fromisoformat(self.joined_on)
        return self


# Punchedin and punchedout timings in Epoch miliseconds

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None

# Data model for Regularized data for maintainig history
class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_null(self):
        for f in ("status", "punch_in", "punch_out"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} must not be null")
        return self


# --------------------------------------------------------------------------- #
# Query / pipeline builders
# --------------------------------------------------------------------------- #
# building queries
def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    d_from = parse_date_str(date_from, "date_from") if date_from is not None else None
    d_to = parse_date_str(date_to, "date_to") if date_to is not None else None
    if d_from and d_to and d_from > d_to:
        raise unprocessable("date_from must not be after date_to", ("query", "date_from"))
    if d_from or d_to:
        q["date"] = {}
        if d_from:
            q["date"]["$gte"] = d_from.isoformat()
        if d_to:
            q["date"]["$lte"] = d_to.isoformat()
    if status is not None:
        q["status"] = status
    return q

# Sorting choices
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def _cond(expr):
    return {"$cond": [expr, 1, 0]}


def _is_present(p):
    return {"$in": [{"$ifNull": [p + "status", ""]}, PRESENT_STATUSES]}


def _is_weekday(p):
    return {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": [p + "date", "1970-01-01"]}}}}, WEEKDAYS]}


def _present_value(p, weekday_only):
    cond = {"$and": [_is_present(p), _is_weekday(p)]} if weekday_only else _is_present(p)
    return {"$cond": [cond, {"$cond": [{"$eq": [p + "half_day", True]}, 0.5, 1]}, 0]}


def _is_late(p):
    return _cond({"$gt": [{"$ifNull": [p + "late_minutes", 0]}, 0]})

# building monthly data track via pipeline
def build_monthly_pipeline(emp_code: str, first: date_type, last: date_type) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _present_value("$", True)},
            "leave_days": {"$sum": _cond({"$eq": ["$status", "LEAVE"]})},
            "late_count": {"$sum": _is_late("$")},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]

# record returned on department matches  
def build_dept_summary_pipeline(first: date_type, last: date_type, department: Optional[str]) -> list:
    match = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    L = "$logs."
    has_hours = {"$and": [_is_present(L), {"$in": [{"$type": L + "work_hours"}, ["double", "int", "long", "decimal"]]}]}
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "work_hours": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "emps": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": _present_value(L, True)},
            "hours_sum": {"$sum": {"$cond": [has_hours, L + "work_hours", 0]}},
            "hours_n": {"$sum": _cond(has_hours)},
            "late_count": {"$sum": _is_late(L)},
            "total_late_minutes": {"$sum": {"$ifNull": [L + "late_minutes", 0]}},
            "leave_count": {"$sum": _cond({"$eq": [{"$ifNull": [L + "status", ""]}, "LEAVE"]})},
            "on_duty_count": {"$sum": _cond({"$eq": [{"$ifNull": [L + "status", ""]}, "ON_DUTY"]})},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$emps"},
            "present_days": 1,
            "hours_sum": 1,
            "hours_n": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]

# function for leaderboard
def build_leaderboard_pipeline(first: date_type, last: date_type, department: Optional[str]) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return pipeline

# parse range for trend limit search
def parse_range(from_s: str, to_s: str):
    d_from = parse_date_str(from_s, "from")
    d_to = parse_date_str(to_s, "to")
    if d_to < d_from:
        raise unprocessable("to must not be before from", ("query", "to"))
    if (d_to - d_from).days + 1 > 92:
        raise unprocessable("range must not exceed 92 days", ("query", "to"))
    return d_from, d_to

# extracting trends from data
def build_trend_pipeline(department: str, d_from: date_type, d_to: date_type) -> list:
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    codes = [e["emp_code"] for e in emps]
    joined = [joined_date(e).isoformat() for e in emps]
    lo = datetime(d_from.year, d_from.month, d_from.day)
    hi = datetime(d_to.year, d_to.month, d_to.day) + timedelta(days=1)
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": _present_value("$", False)}, "late_count": {"$sum": _is_late("$")}}},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$addFields": {
            "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, WEEKDAYS]},
            "headcount": {"$size": {"$filter": {"input": {"$literal": joined}, "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}},
        }},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1}},
    ]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    # added MongoDB connection check instead of just sending status OK
    try:
        db.command("ping")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Mongo Connection failure: {exc}") from exc
    if not _indexes_ready:
        try:
            ensure_indexes()
        except Exception as exc:
            log.warning("index creation failed: %s", exc)
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    now = now_utc_ms()
    doc["created_at"] = now
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    doc.pop("_id", None)
    doc["created_at"] = to_millis(now)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    # fixed skip by changing `page` to `page - 1`
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    # Added sorting ascending order
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    return {"items": [emp_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    # Handled for unregistered employee
    if not emp:
        raise HTTPException(404, "unknown employee")
    if body.punched_at is not None:
        ts = truncate_to_seconds(from_millis(body.punched_at))
    else:
        ts = truncate_to_seconds(datetime.now(IST))
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return record_out(doc)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[Status] = None,
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)
):
    # Replaced code with function build query
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = db.attendance_logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# Punch out API
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    if body.punched_at is not None:
        ts = truncate_to_seconds(from_millis(body.punched_at))
    else:
        ts = truncate_to_seconds(datetime.now(IST))
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    punch_in_time = truncate_to_seconds(rec["punch_in"])
    if ts <= punch_in_time:
        raise unprocessable("punched_at must be after punch_in", ("body", "punched_at"))
    if ts - punch_in_time > timedelta(hours=24):
        raise unprocessable("punched_at is more than 24 hours after punch_in", ("body", "punched_at"))
    work = compute_work_hours(punch_in_time, ts)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": work,
            "overtime_minutes": compute_overtime(ts, emp["shift_end"], rec["date"], emp["shift_start"]),
            "half_day": compute_halfday(work),
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


# API for attendence with specific employee code and date
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date_str(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no record for that date")

    sent = body.model_fields_set
    old_status, old_in, old_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status if "status" in sent else old_status

    if new_status in ("ABSENT", "LEAVE"):
        if "punch_in" in sent or "punch_out" in sent:
            raise unprocessable("ABSENT/LEAVE records cannot have punch times")
        new_in = new_out = None
    else:
        new_in = truncate_to_seconds(from_millis(body.punch_in)) if "punch_in" in sent else old_in
        new_out = truncate_to_seconds(from_millis(body.punch_out)) if "punch_out" in sent else old_out
        if new_in is None:
            raise unprocessable("a presence status requires punch_in")
        if "punch_in" in sent and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise unprocessable("punch_in must stay on the record's attendance date", ("body", "punch_in"))
        if ("punch_in" in sent or "punch_out" in sent) and new_out is not None:
            if new_out <= new_in or new_out - new_in > timedelta(hours=24):
                raise unprocessable("punch_out must be after punch_in and within 24 hours", ("body", "punch_out"))

    if (new_status, new_in, new_out) == (old_status, old_in, old_out):
        raise unprocessable("request changes nothing")

    if new_in is not None:
        late = compute_late_minutes(new_in, emp["shift_start"], date)
    else:
        late = 0
    if new_in is not None and new_out is not None:
        work = compute_work_hours(new_in, new_out)
        ot = compute_overtime(new_out, emp["shift_end"], date, emp["shift_start"])
        half = compute_halfday(work)
    else:
        work, ot, half = None, 0, False

    new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out, "work_hours": work, "late_minutes": late, "overtime_minutes": ot, "half_day": half}
    old_vals = {"status": old_status, "punch_in": old_in, "punch_out": old_out, "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0, "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False))}
    changes = {k: {"from": old_vals[k], "to": new_vals[k]} for k in new_vals if new_vals[k] != old_vals[k]}
    entry = {"at": now_utc_ms(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    flt = {"emp_code": emp_code, "date": date, "status": old_status, "punch_in": old_in, "punch_out": old_out}
    hist = rec.get("history")
    if isinstance(hist, list):
        flt["history"] = {"$size": len(hist)}
        update = {"$set": new_vals, "$push": {"history": entry}}
    else:
        flt["history"] = None
        update = {"$set": {**new_vals, "history": [entry]}}
    updated = db.attendance_logs.find_one_and_update(flt, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, retry")
    return record_out(updated)

# API to view the anlaytics of the month for employee code
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    first, last = parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    working_days = count_working_days(max(first, joined_date(emp)), last)
    rows = list(db.attendance_logs.aggregate(build_monthly_pipeline(emp_code, first, last)))
    s = rows[0] if rows else {}
    present_days = round_half_up(s.get("present_days", 0), 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(s.get("leave_days", 0)),
        "late_count": int(s.get("late_count", 0)),
        "total_late_minutes": int(s.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(s.get("total_overtime_minutes", 0)),
        "attendance_pct": float(ratio_dec(present_days * 100, working_days, 2)) if working_days else None,
    }

# Analytical summary of departments
@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    first, last = parse_month(month)
    pipeline = build_dept_summary_pipeline(first, last, department)
    rows = db.employees.aggregate(pipeline, hint=EMP_DEPT_JOINED_HINT)
    items = []
    for r in rows:
        n = int(r["hours_n"])
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": float(ratio_dec(r["hours_sum"], n, 2)) if n else None,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}

# API for leaderboard of late ones
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = parse_month(month)
    rows = db.attendance_logs.aggregate(build_leaderboard_pipeline(first, last, department))
    items, rank, prev = [], 0, None
    for i, r in enumerate(rows, start=1):
        if r["total_late_minutes"] != prev:
            rank, prev = i, r["total_late_minutes"]
        if rank > limit:
            break
        items.append({
            "rank": rank,
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        })
    return {"month": month, "items": items}

# API for analytics by department trends
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    d_from, d_to = parse_range(from_date, to_date)
    rows = list(db.attendance_logs.aggregate(build_trend_pipeline(department, d_from, d_to)))
    rates = []
    for r in rows:
        if r["is_working_day"] and r["headcount"] > 0:
            rates.append(ratio_dec(r["present_count"], r["headcount"], 4))
        else:
            rates.append(None)
    items = []
    for i, r in enumerate(rows):
        window = [x for x in rates[max(0, i - 6): i + 1] if x is not None]
        moving = (sum(window) / len(window)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) if window else None
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": float(rates[i]) if rates[i] is not None else None,
            "moving_avg_7d": float(moving) if moving is not None else None,
        })
    return {"department": department, "items": items}


ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _require(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, [{"type": "missing", "loc": ["query", k], "msg": "Field required", "input": None} for k in missing])


# API for explain endpoint
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        coll = "attendance_logs"
        q = build_attendance_query(emp_code, date_from, date_to, status)
        inner = {"find": coll, "filter": q, "sort": dict(ATTENDANCE_SORT), "skip": (page - 1) * page_size, "limit": page_size}
    elif endpoint == "employee_monthly":
        _require(emp_code=emp_code, month=month)
        first, last = parse_month(month)
        coll = "attendance_logs"
        inner = {"aggregate": coll, "pipeline": build_monthly_pipeline(emp_code, first, last), "cursor": {}}
    elif endpoint == "department_summary":
        _require(month=month)
        first, last = parse_month(month)
        coll = "employees"
        inner = {"aggregate": coll, "pipeline": build_dept_summary_pipeline(first, last, department), "cursor": {}, "hint": dict(EMP_DEPT_JOINED_HINT)}
    elif endpoint == "late_leaderboard":
        _require(month=month)
        first, last = parse_month(month)
        coll = "attendance_logs"
        inner = {"aggregate": coll, "pipeline": build_leaderboard_pipeline(first, last, department), "cursor": {}}
    else:
        _require(department=department, **{"from": from_date, "to": to_date})
        d_from, d_to = parse_range(from_date, to_date)
        coll = "attendance_logs"
        inner = {"aggregate": coll, "pipeline": build_trend_pipeline(department, d_from, d_to), "cursor": {}}

    result = db.command({"explain": inner, "verbosity": "executionStats"})
    plain = json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": coll, "explain": plain}
