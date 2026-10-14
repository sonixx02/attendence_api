"""Employee Attendance & Analytics API.

The assignment deliberately keeps the service in this file.  The implementation
focuses on the contract, atomic MongoDB writes, and aggregation-backed reports.
"""
import os
import re
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MILLISECONDS_PER_SECOND = 1000
SECONDS_PER_HOUR = 3600
MAX_PUNCH_SECONDS = 24 * 60 * 60
LATE_GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_HOURS = Decimal("4.50")
WORK_HOURS_PLACES = "0.01"
RATE_PLACES = "0.0001"
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=UTC,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _parse_date(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("must be a valid YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("must be a valid YYYY-MM-DD date") from exc


def _date(value: str) -> date:
    try:
        return _parse_date(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _month(value: str) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(422, "must be YYYY-MM")
    return value


def _time(value: str) -> str:
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("must be HH:MM")
    return value



def _epoch(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise ValueError("must be an epoch millisecond integer in the allowed range")
    return datetime.fromtimestamp(value / MILLISECONDS_PER_SECOND, UTC).replace(microsecond=0)


def _ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.replace(microsecond=0)
    return int(value.timestamp() * MILLISECONDS_PER_SECOND)


def _round(value: Decimal, places: str) -> float:
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _shift_dt(day: date, value: str) -> datetime:
    hour, minute = map(int, value.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def _shift_start(punch: datetime, shift_start: str, shift_end: str) -> datetime:
    local = punch.astimezone(IST)
    start_day = local.date()
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        start_day -= timedelta(days=1)
    return _shift_dt(start_day, shift_start)


def _shift_end(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    day = _date(attendance_date)
    result = _shift_dt(day, shift_end)
    if shift_end <= shift_start:
        result += timedelta(days=1)
    return result


def _attendance_date(punch: datetime, shift_start: str, shift_end: str) -> str:
    local = punch.astimezone(IST)
    day = local.date()
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        day -= timedelta(days=1)
    return day.isoformat()


def _late(punch: datetime, shift_start: str, shift_end: str) -> int:
    seconds = (punch.astimezone(IST) - _shift_start(punch, shift_start, shift_end)).total_seconds()
    return int(seconds // 60) if seconds > LATE_GRACE_SECONDS else 0


def _work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    return _round(seconds / Decimal(str(SECONDS_PER_HOUR)), WORK_HOURS_PLACES)


def _overtime(punch_out: datetime, attendance_date: str, shift_start: str, shift_end: str) -> int:
    seconds = (punch_out.astimezone(IST) - _shift_end(attendance_date, shift_start, shift_end)).total_seconds()
    minutes = int(seconds // 60)
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def _derived(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
             employee: dict[str, Any], attendance_date: str) -> dict[str, Any]:
    if status not in PRESENCE:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if punch_in is None:
        raise HTTPException(422, "presence status requires punch_in")
    work_hours = _work_hours(punch_in, punch_out) if punch_out else None
    return {
        "work_hours": work_hours,
        "late_minutes": _late(punch_in, employee["shift_start"], employee["shift_end"]),
        "overtime_minutes": _overtime(punch_out, attendance_date, employee["shift_start"], employee["shift_end"]) if punch_out else 0,
        "half_day": work_hours is not None and Decimal(str(work_hours)) < HALF_DAY_HOURS,
    }


def _record(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _ms(doc.get("punch_in")),
        "punch_out": _ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [_history(x) for x in doc.get("history", [])],
    }


def _history(entry: dict[str, Any]) -> dict[str, Any]:
    changes = {}
    for key, value in entry.get("changes", {}).items():
        changes[key] = {
            "from": _ms(value.get("from")) if key in {"punch_in", "punch_out"} else value.get("from"),
            "to": _ms(value.get("to")) if key in {"punch_in", "punch_out"} else value.get("to"),
        }
    return {"at": _ms(entry["at"]), "by": entry["by"], "reason": entry["reason"], "changes": changes}


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30")
    shift_end: str = Field(default="18:30")
    joined_on: str

    @field_validator("shift_start", "shift_end") 
    # for my own check thus @field validator hence we use calssmethod also which uses what pydantic provides /expects  cls refers to model class value is the field valur to be checked 
    @classmethod
    def valid_time(cls, value: str) -> str:
        return _time(value)

    @field_validator("joined_on")
    @classmethod
    def valid_date(cls, value: str) -> str:
        _parse_date(value)
        return value

    @model_validator(mode="after")
    def different_shifts(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_epoch(cls, value: Optional[int]) -> Optional[int]:
        _epoch(value)
        return value


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def valid_epoch(cls, value: Optional[int]) -> Optional[int]:
        _epoch(value)
        return value


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_epoch(cls, value: Optional[int]) -> Optional[int]:
        _epoch(value)
        return value


# --------------------------------------------------------------------------- #
# Application startup
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(_: FastAPI):
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_employee")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="status_date")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB is unavailable") from exc


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": _ms(doc["created_at"]),
    }


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query: dict[str, Any] = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    items = []
    for doc in docs:
        doc["created_at"] = _ms(doc.get("created_at"))
        items.append(doc)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    instant = _epoch(body.punched_at) or datetime.now(UTC).replace(microsecond=0)
    attendance_date = _attendance_date(instant, employee["shift_start"], employee["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": attendance_date, "status": body.status,
        "punch_in": instant, "punch_out": None, "history": [],
    }
    doc.update(_derived(body.status, instant, None, employee, attendance_date))
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for that date") from exc
    return _record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    instant = _epoch(body.punched_at) or datetime.now(UTC).replace(microsecond=0)
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "status": {"$in": list(PRESENCE)}, "punch_in": {"$lte": instant}},
        sort=[("punch_in", DESCENDING)],
    )
    if candidate is None:
        raise HTTPException(404, "no punch-in found")
    if candidate.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    punch_in_value = candidate["punch_in"]
    elapsed = (instant - punch_in_value).total_seconds()
    if elapsed <= 0 or elapsed > MAX_PUNCH_SECONDS:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    derived = _derived(candidate["status"], punch_in_value, instant, employee, candidate["date"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {"$set": {"punch_out": instant, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record is already punched out")
    return _record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
):
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None:
        _date(date_from)
        query.setdefault("date", {})["$gte"] = date_from
    if date_to is not None:
        _date(date_to)
        query.setdefault("date", {})["$lte"] = date_to
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not exceed date_to")
    if status is not None:
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [_record(doc) for doc in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Endpoint 
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    _date(date)
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if current is None:
        raise HTTPException(404, "attendance record not found")
    status = body.status if body.status is not None else current["status"]
    incoming_in = _epoch(body.punch_in) if body.punch_in is not None else current.get("punch_in")
    incoming_out = _epoch(body.punch_out) if body.punch_out is not None else current.get("punch_out")
    if status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "absence or leave cannot have punch times")
        incoming_in = incoming_out = None
    elif incoming_in is None:
        raise HTTPException(422, "presence status requires punch_in")
    if incoming_in is not None:
        if _attendance_date(incoming_in, employee["shift_start"], employee["shift_end"]) != date:
            raise HTTPException(422, "punch_in does not belong to the record date")
    if incoming_out is not None:
        if incoming_in is None or incoming_out <= incoming_in or (incoming_out - incoming_in).total_seconds() > 86400:
            raise HTTPException(422, "invalid punch_out")
    derived = _derived(status, incoming_in, incoming_out, employee, date)
    changed = {}
    current_defaults = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    for key, new_value in {"status": status, "punch_in": incoming_in, "punch_out": incoming_out, **derived}.items():
        old_value = current.get(key, current_defaults.get(key))
        if old_value != new_value:
            changed[key] = {"from": old_value, "to": new_value}
    if not changed:
        raise HTTPException(422, "request changes nothing")
    history = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changed}
    update = {"$set": {"status": status, "punch_in": incoming_in, "punch_out": incoming_out, **derived}, "$push": {"history": history}}
    old_history = current.get("history", [])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": current["_id"], "status": current["status"],
         "punch_in": current.get("punch_in"), "punch_out": current.get("punch_out"),
         "$expr": {"$eq": [
             {"$size": {"$ifNull": ["$history", []]}}, len(old_history)
         ]}},
        update,
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "concurrent regularization conflict")
    return _record(updated)


# --------------------------------------------------------------------------- #
# Analytics helpers
# --------------------------------------------------------------------------- #
def _month_bounds(month: str) -> tuple[date, date]:
    _month(month)
    start = date.fromisoformat(month + "-01")
    end = (date(start.year + (start.month == 12), (start.month % 12) + 1, 1) - timedelta(days=1))
    return start, end


def _working_days(joined_on: str, month: str) -> int:
    start, end = _month_bounds(month)
    current = max(start, _date(joined_on))
    return sum(1 for i in range((end - current).days + 1) if (current + timedelta(days=i)).weekday() < 5)


def _month_pipeline(emp_code: str, month: str, working_days: int) -> list[dict[str, Any]]:
    start, end = _month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$set": {
            "is_present": {"$in": ["$status", list(PRESENCE)]},
            "is_weekday": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
            "half_day_value": {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
            "late_value": {"$ifNull": ["$late_minutes", 0]},
            "overtime_value": {"$ifNull": ["$overtime_minutes", 0]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": ["$is_present", "$is_weekday"]}, "$half_day_value", 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_value", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_value"},
            "total_overtime_minutes": {"$sum": "$overtime_value"},
        }},
        {"$project": {"_id": 0}},
        {"$set": {"attendance_pct": {"$cond": [
            {"$gt": [working_days, 0]},
            {"$multiply": [{"$divide": ["$present_days", working_days]}, 100]},
            None,
        ]}}},
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(404, "employee not found")
    working_days = _working_days(employee["joined_on"], month)
    rows = list(db.attendance_logs.aggregate(_month_pipeline(emp_code, month, working_days)))
    values = rows[0] if rows else {
        "present_days": 0, "leave_days": 0, "late_count": 0,
        "total_late_minutes": 0, "total_overtime_minutes": 0,
    }
    result = {
        "emp_code": emp_code, "month": month,
        "working_days": working_days,
        "present_days": values.get("present_days", 0),
        "leave_days": values.get("leave_days", 0),
        "late_count": values.get("late_count", 0),
        "total_late_minutes": values.get("total_late_minutes", 0),
        "total_overtime_minutes": values.get("total_overtime_minutes", 0),
    }
    result["attendance_pct"] = None if not result["working_days"] else _round(
        Decimal(str(values.get("attendance_pct", 0))), RATE_PLACES)
    return result


def _department_pipeline(month: str, department: Optional[str] = None) -> list[dict[str, Any]]:
    start, end = _month_bounds(month)
    match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]},
                {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}},
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$emp_code",
            "department": {"$first": "$department"},
            "logs": {"$push": "$logs"},
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$set": {
            "present": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", list(PRESENCE)]},
                    {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                ]},
                {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]},
                False,
            ]},
            "present_value": {"$cond": [
                {"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1,
            ]},
        }},
        {"$group": {
            "_id": "$department",
            "employee_codes": {"$addToSet": "$_id"},
            "present_days": {"$sum": {"$cond": ["$present", "$present_value", 0]}},
            "avg_work_hours": {"$avg": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", list(PRESENCE)]},
                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                ]},
                "$logs.work_hours",
                None,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$set": {"headcount": {"$size": "$employee_codes"}}},
        {"$project": {"employee_codes": 0}},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    pipeline = _department_pipeline(month, department)
    rows = list(db.employees.aggregate(pipeline))
    items = []
    for row in rows:
        item = {
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": row.get("present_days", 0),
            "avg_work_hours": row.get("avg_work_hours"),
            "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0),
            "leave_count": row.get("leave_count", 0),
            "on_duty_count": row.get("on_duty_count", 0),
        }
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = _round(
                Decimal(str(item["avg_work_hours"])), WORK_HOURS_PLACES
            )
        items.append(item)
    return {"month": month, "items": items}


def _leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None) -> list[dict[str, Any]]:
    start, end = _month_bounds(month)
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                              "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    return {"month": month, "items": list(db.attendance_logs.aggregate(
        _leaderboard_pipeline(month, limit, department)
    ))}


def _trend_pipeline(department: str, from_date: str, to_date: str) -> list[dict[str, Any]]:
    start, end = _date(from_date), _date(to_date)
    return [
        {"$documents": [
            {"date": datetime(start.year, start.month, start.day), "present_count": 0, "late_count": 0},
            {"date": datetime(end.year, end.month, end.day), "present_count": 0, "late_count": 0},
        ]},
        {"$unionWith": {"coll": "attendance_logs", "pipeline": [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
            {"$match": {"employee.department": department}},
            {"$set": {"day": {"$dateFromString": {"dateString": "$date"}}}},
            {"$group": {"_id": "$day",
                    "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
            {"$project": {"_id": 0, "date": "$_id", "present_count": 1, "late_count": 1}},
        ]}},
        {"$group": {"_id": "$date", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$project": {"_id": 0, "date": "$_id", "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day",
                    "bounds": [datetime(start.year, start.month, start.day),
                               datetime(end.year, end.month, end.day) + timedelta(days=1)]}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$department", department]},
                {"$lte": ["$joined_on", {"$dateToString": {
                    "date": "$$day", "format": "%Y-%m-%d"
                }}]},
            ]}}},
            {"$count": "count"},
        ], "as": "headcount_docs"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_docs.count", 0]}, 0]}}},
        {"$set": {
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$set": {"is_working_day": {"$in": [{"$dayOfWeek": "$date"}, [2, 3, 4, 5, 6]]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate",
            "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d"}},
                      "is_working_day": 1, "headcount": 1, "present_count": {"$ifNull": ["$present_count", 0]},
                      "late_count": {"$ifNull": ["$late_count", 0]}, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]
    

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(alias="from"), to_date: str = Query(alias="to")):
    start, end = _date(from_date), _date(to_date)
    if end < start:
        raise HTTPException(422, "to must not be before from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")
    items = list(db.aggregate(_trend_pipeline(department, from_date, to_date)))
    for item in items:
        if item["attendance_rate"] is not None:
            item["attendance_rate"] = _round(
                Decimal(str(item["attendance_rate"])), RATE_PLACES
            )
        if item["moving_avg_7d"] is not None:
            item["moving_avg_7d"] = _round(
                Decimal(str(item["moving_avg_7d"])), RATE_PLACES
            )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin endpoints
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
            department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
            date_from: Optional[str] = None, date_to: Optional[str] = None,
            status: Optional[str] = None, from_date: Optional[str] = Query(None, alias="from"),
            to_date: Optional[str] = Query(None, alias="to"), page: int = Query(1, ge=1),
            page_size: int = Query(20, ge=1, le=100)):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(422, "unknown endpoint")
    if endpoint == "attendance_list":
        if date_from:
            _date(date_from)
        if date_to:
            _date(date_to)
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not exceed date_to")
        if status and status not in {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}:
            raise HTTPException(422, "invalid status")
        query: dict[str, Any] = {}
        if emp_code: query["emp_code"] = emp_code
        if status: query["status"] = status
        if date_from or date_to:
            query["date"] = {}
            if date_from: query["date"]["$gte"] = date_from
            if date_to: query["date"]["$lte"] = date_to
        explain_doc = db.command("explain", {"find": "attendance_logs", "filter": query,
                                             "sort": {"date": -1, "emp_code": 1},
                                             "skip": (page - 1) * page_size, "limit": page_size}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": json.loads(json_util.dumps(explain_doc))}
    if endpoint in {"employee_monthly", "department_summary", "late_leaderboard"} and not month:
        raise HTTPException(422, "month is required")
    if endpoint == "employee_monthly" and not emp_code:
        raise HTTPException(422, "emp_code is required")
    if endpoint == "department_trend" and not (department and from_date and to_date):
        raise HTTPException(422, "department, from and to are required")
    if endpoint == "department_summary":
        pipeline = _department_pipeline(month, department)
        collection = "employees"
    elif endpoint == "employee_monthly":
        employee = db.employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
        if employee is None:
            raise HTTPException(404, "employee not found")
        pipeline = _month_pipeline(emp_code, month, _working_days(employee["joined_on"], month))
        collection = "attendance_logs"
    elif endpoint == "late_leaderboard":
        pipeline = _leaderboard_pipeline(month, limit, department)
        collection = "attendance_logs"
    else:
        start, end = _date(from_date), _date(to_date)
        if end < start:
            raise HTTPException(422, "to must not be before from")
        if (end - start).days + 1 > 92:
            raise HTTPException(422, "range cannot exceed 92 days")
        pipeline = _trend_pipeline(department, from_date, to_date)
        # The first stage is $documents, so this collection is only the
        # required namespace for the aggregate command.
        collection = "1"
    if endpoint == "department_trend":
        # MongoDB requires aggregate: 1 for a pipeline beginning with
        # $documents; the aggregate command's explain flag is the supported
        # form for this special namespace.
        explain_doc = db.command(
            "aggregate", 1, pipeline=pipeline, cursor={}, explain=True
        )
    else:
        explain_doc = db.command(
            "explain",
            {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
    return {"endpoint": endpoint, "collection": collection,
            "explain": json.loads(json_util.dumps(explain_doc))}
