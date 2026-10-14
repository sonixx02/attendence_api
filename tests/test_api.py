import unittest
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app, db


def epoch_ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


def explain_stages(value):
    stages = []
    if isinstance(value, dict):
        if "stage" in value:
            stages.append(value["stage"])
        for child in value.values():
            stages.extend(explain_stages(child))
    elif isinstance(value, list):
        for child in value:
            stages.extend(explain_stages(child))
    return stages


class AttendanceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        cls.employee_codes = []

    @classmethod
    def tearDownClass(cls):
        if cls.employee_codes:
            db.employees.delete_many({"emp_code": {"$in": cls.employee_codes}})
            db.attendance_logs.delete_many({"emp_code": {"$in": cls.employee_codes}})
        cls.client_context.__exit__(None, None, None)

    def create_employee(self, *, shift_start="09:30", shift_end="18:30", department=None):
        code = f"EMP{uuid4().int % 1_000_000:06d}"
        self.employee_codes.append(code)
        body = {
            "emp_code": code,
            "name": "Verification Employee",
            "email": f"{code.lower()}@example.com",
            "department": department or f"Verification-{code}",
            "shift_start": shift_start,
            "shift_end": shift_end,
            "joined_on": "2026-07-01",
        }
        response = self.client.post("/employees", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return code

    def test_health_and_employee_crud(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        code = self.create_employee()

        duplicate = self.client.post(
            "/employees",
            json={
                "emp_code": code,
                "name": "Duplicate",
                "email": "duplicate@example.com",
                "department": "Verification",
                "joined_on": "2026-07-01",
            },
        )
        self.assertEqual(duplicate.status_code, 409)

        page = self.client.get("/employees", params={"department": f"Verification-{code}", "page_size": 1})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.json()["total"], 1)
        self.assertEqual(page.json()["items"][0]["emp_code"], code)

    def test_validation_rejects_seconds_and_equal_shifts(self):
        invalid_timestamp = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": 1719811200},
        )
        self.assertEqual(invalid_timestamp.status_code, 422)

        equal_shifts = self.client.post(
            "/employees",
            json={
                "emp_code": f"EMP{uuid4().int % 1_000_000:06d}",
                "name": "Invalid Shift",
                "email": "invalid-shift@example.com",
                "department": "Verification",
                "shift_start": "09:00",
                "shift_end": "09:00",
                "joined_on": "2026-07-01",
            },
        )
        self.assertEqual(equal_shifts.status_code, 422)

    def test_late_boundary_and_punch_out_calculations(self):
        code = self.create_employee()
        punch_in = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": code, "punched_at": epoch_ms("2026-07-20T04:10:01")},
        )
        self.assertEqual(punch_in.status_code, 201, punch_in.text)
        self.assertEqual(punch_in.json()["date"], "2026-07-20")
        self.assertEqual(punch_in.json()["late_minutes"], 10)

        punch_out = self.client.post(
            "/attendance/punch-out",
            json={"emp_code": code, "punched_at": epoch_ms("2026-07-20T13:35:00")},
        )
        self.assertEqual(punch_out.status_code, 200, punch_out.text)
        self.assertEqual(punch_out.json()["overtime_minutes"], 35)
        self.assertEqual(punch_out.json()["work_hours"], 9.42)
        self.assertFalse(punch_out.json()["half_day"])

    def test_overnight_shift_date_and_overtime(self):
        code = self.create_employee(shift_start="22:00", shift_end="06:00")
        punch_in = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": code, "punched_at": epoch_ms("2026-07-20T20:00:00")},
        )
        self.assertEqual(punch_in.status_code, 201, punch_in.text)
        self.assertEqual(punch_in.json()["date"], "2026-07-20")
        self.assertEqual(punch_in.json()["late_minutes"], 210)

        punch_out = self.client.post(
            "/attendance/punch-out",
            json={"emp_code": code, "punched_at": epoch_ms("2026-07-21T01:05:00")},
        )
        self.assertEqual(punch_out.status_code, 200, punch_out.text)
        self.assertEqual(punch_out.json()["overtime_minutes"], 35)
        self.assertEqual(punch_out.json()["work_hours"], 5.08)

    def test_regularize_appends_history_and_legacy_noop_is_rejected(self):
        code = self.create_employee()
        db.attendance_logs.insert_one({
            "emp_code": code,
            "date": "2026-07-14",
            "status": "PRESENT",
            "punch_in": datetime.fromisoformat("2026-07-14T04:00:00").replace(tzinfo=timezone.utc),
            "punch_out": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "history": [],
        })

        noop = self.client.patch(
            f"/attendance/{code}/2026-07-14",
            json={"reason": "No actual change", "regularized_by": "test"},
        )
        self.assertEqual(noop.status_code, 422)
        self.assertEqual(noop.json()["detail"], "request changes nothing")

        correction = self.client.patch(
            f"/attendance/{code}/2026-07-14",
            json={
                "punch_in": epoch_ms("2026-07-14T04:20:00"),
                "reason": "Corrected arrival",
                "regularized_by": "test",
            },
        )
        self.assertEqual(correction.status_code, 200, correction.text)
        self.assertEqual(len(correction.json()["history"]), 1)
        self.assertIn("punch_in", correction.json()["history"][0]["changes"])

    def test_trend_gap_filling_and_monthly_shape(self):
        department = f"Trend-{uuid4().hex[:8]}"
        code = self.create_employee(department=department)
        self.client.post(
            "/attendance/punch-in",
            json={"emp_code": code, "punched_at": epoch_ms("2026-07-20T04:00:00")},
        )
        trend = self.client.get(
            f"/analytics/departments/{department}/trend",
            params={"from": "2026-07-18", "to": "2026-07-22"},
        )
        self.assertEqual(trend.status_code, 200, trend.text)
        items = trend.json()["items"]
        self.assertEqual([item["date"] for item in items], [
            "2026-07-18", "2026-07-19", "2026-07-20", "2026-07-21", "2026-07-22"
        ])
        self.assertFalse(items[0]["is_working_day"])
        self.assertEqual(items[2]["present_count"], 1)
        self.assertEqual(items[0]["attendance_rate"], None)
        self.assertEqual(items[3]["present_count"], 0)
        self.assertEqual(items[3]["attendance_rate"], 0.0)

        monthly = self.client.get(
            f"/analytics/employees/{code}/monthly",
            params={"month": "2026-07"},
        )
        self.assertEqual(monthly.status_code, 200, monthly.text)
        self.assertEqual(monthly.json()["emp_code"], code)
        self.assertIn("attendance_pct", monthly.json())

    def test_summary_counts_no_log_employee_and_joiner_days(self):
        department = f"Summary-{uuid4().hex[:8]}"
        code = self.create_employee(department=department)
        employee = db.employees.find_one({"emp_code": code})
        self.assertIsNotNone(employee)

        summary = self.client.get(
            "/analytics/departments/summary",
            params={"month": "2026-07", "department": department},
        )
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertEqual(summary.json()["items"][0]["headcount"], 1)

        monthly = self.client.get(
            f"/analytics/employees/{code}/monthly",
            params={"month": "2026-07"},
        )
        self.assertEqual(monthly.status_code, 200, monthly.text)
        self.assertEqual(monthly.json()["working_days"], 23)
        self.assertEqual(monthly.json()["present_days"], 0)

    def test_leaderboard_tie_at_limit_returns_all_tied_rows(self):
        department = f"Ranking-{uuid4().hex[:8]}"
        codes = [self.create_employee(department=department) for _ in range(2)]
        for code in codes:
            db.attendance_logs.insert_one({
                "emp_code": code,
                "date": "2026-07-15",
                "status": "PRESENT",
                "punch_in": datetime.fromisoformat("2026-07-15T05:00:00").replace(tzinfo=timezone.utc),
                "punch_out": None,
                "late_minutes": 90,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [],
            })

        leaderboard = self.client.get(
            "/analytics/leaderboard/late",
            params={"month": "2026-07", "department": department, "limit": 1},
        )
        self.assertEqual(leaderboard.status_code, 200, leaderboard.text)
        rows = leaderboard.json()["items"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["rank"] for row in rows}, {1})
        self.assertEqual([row["emp_code"] for row in rows], sorted(codes))

    def test_department_summary_and_explain_use_indexes(self):
        summary = self.client.get("/analytics/departments/summary", params={"month": "2026-07"})
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertTrue(all("headcount" in item for item in summary.json()["items"]))

        explained = self.client.get(
            "/admin/explain/department_summary",
            params={"month": "2026-07"},
        )
        self.assertEqual(explained.status_code, 200, explained.text)
        stages = explain_stages(explained.json()["explain"])
        self.assertIn("IXSCAN", stages)
        self.assertNotIn("COLLSCAN", stages)

    def test_explain_endpoint_covers_all_supported_targets(self):
        code = "EMP0001"
        requests = {
            "attendance_list": {"date_from": "2026-07-01", "date_to": "2026-07-31"},
            "employee_monthly": {"emp_code": code, "month": "2026-07"},
            "department_summary": {"month": "2026-07"},
            "late_leaderboard": {"month": "2026-07"},
            "department_trend": {
                "department": "Engineering", "from": "2026-07-01", "to": "2026-07-03"
            },
        }
        for endpoint, params in requests.items():
            with self.subTest(endpoint=endpoint):
                response = self.client.get(f"/admin/explain/{endpoint}", params=params)
                self.assertEqual(response.status_code, 200, response.text)
                stages = explain_stages(response.json()["explain"])
                self.assertIn("IXSCAN", stages)
                self.assertNotIn("COLLSCAN", stages)

    def test_attendance_pagination_and_status_filter(self):
        response = self.client.get(
            "/attendance",
            params={"status": "PRESENT", "page": 1, "page_size": 2},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(len(body["items"]), min(2, body["total"]))
        self.assertTrue(all(item["status"] == "PRESENT" for item in body["items"]))

    def test_concurrent_punch_in_and_punch_out_are_race_safe(self):
        code = self.create_employee()
        punch_in_body = {
            "emp_code": code,
            "punched_at": epoch_ms("2026-07-23T04:00:00"),
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            punch_in_results = list(pool.map(
                lambda _: self.client.post("/attendance/punch-in", json=punch_in_body),
                range(2),
            ))
        self.assertEqual(sorted(response.status_code for response in punch_in_results), [201, 409])

        punch_out_body = {
            "emp_code": code,
            "punched_at": epoch_ms("2026-07-23T13:00:00"),
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            punch_out_results = list(pool.map(
                lambda _: self.client.post("/attendance/punch-out", json=punch_out_body),
                range(2),
            ))
        self.assertEqual(sorted(response.status_code for response in punch_out_results), [200, 409])


if __name__ == "__main__":
    unittest.main()
