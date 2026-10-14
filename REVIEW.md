

The starter covered only the happy path.
The main problems were validation, time handling, pagination, concurrency, and
the missing endpoints.

## Defects found in the starter

| # | Where | What was wrong | How I noticed it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | It converted elapsed seconds to minutes before checking the 10-minute grace boundary. | `09:40:01` was treated as not late, but R2 requires 10 late minutes. | Compare seconds to 600 first, then floor the minutes. |
| 2 | `compute_late_minutes` | It built the shift start on the punch-in calendar date, which is wrong for an overnight shift. | A `22:00-06:00` employee punching in at `01:00` got a future shift start. | Resolve the attendance date first and build the shift start in IST. |
| 3 | `compute_work_hours` | Python `round()` uses banker’s rounding. | A half-way value such as `4.495` hours can round to `4.49`. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | It counted overtime even when it was less than 30 minutes. | An `18:35` punch-out for an `18:30` shift returned 5 overtime minutes. | Return overtime only when the elapsed time is at least 30 minutes. |
| 5 | `compute_overtime` | It put an overnight shift end on the wrong calendar day. | A `22:00-06:00` shift produced almost a full day of false overtime. | Add one day to the shift end for overnight shifts. |
| 6 | Time handling | The starter used local, sometimes naive datetimes. | The API could calculate a different date depending on the server timezone, and aware/naive subtraction could fail. | Convert instants to UTC-aware datetimes and calendar values to IST. |
| 7 | `health` | It always returned 200 without checking MongoDB. | Stopping MongoDB would still produce `{"status": "ok"}`. | Ping MongoDB and return 503 when it is unavailable. |
| 8 | `EmployeeIn` | It did not validate employee fields, dates, shift times, or equal shifts. | Invalid dates, times, and `shift_start == shift_end` were accepted. | Added Pydantic field validation and a model validator. |
| 9 | `create_employee` | The `find_one()` then `insert_one()` check was not race-safe. | Two identical requests could both pass the check. | Added a unique database index and handled `DuplicateKeyError` as 409. |
| 10 | `create_employee` | It stored and returned the server datetime in the wrong API format. | The contract requires epoch milliseconds and truncated seconds. | Store UTC datetimes internally and convert them at the API boundary. |
| 11 | `list_employees` | Page 1 skipped the first page of records. | `skip = page * page_size` starts page 1 at record 21. | Use `(page - 1) * page_size`. |
| 12 | `list_employees` | `total` ignored the department filter. | A filtered request reported the whole collection count. | Count using the same query as the listing. |
| 13 | `list_employees` | It did not guarantee `emp_code` ascending order. | Pagination could change order between requests. | Sort in MongoDB by `emp_code`. |
| 14 | `list_employees` | Page and page-size limits were not validated. | Zero, negative, or oversized values were accepted. | Added FastAPI query constraints. |
| 15 | `punch_in` | It did not handle an unknown employee before accessing shift data. | An unknown employee could cause a server error instead of 404. | Return 404 before using employee fields. |
| 16 | `punch_in` | It accepted seconds, floats, strings, and invalid epoch values. | A seconds timestamp could be interpreted as a 1970 date. | Added strict epoch-millisecond validation. |
| 17 | `punch_in` | It calculated the date using server-local time and ignored overnight rules. | Overnight punches before shift end were assigned to the wrong day. | Use IST and `_attendance_date()`. |
| 18 | `punch_in` | The pre-insert duplicate check was not concurrency-safe. | Parallel punch-ins could both return 201. | Added a unique `(emp_code, date)` index and handled duplicate inserts. |
| 19 | `punch_in` | It returned an internal `id` field. | `_id` and a derived `id` are not part of the contract. | Return only contract fields through `_record()`. |
| 20 | `list_attendance` | It loaded and sorted the entire collection in Python. | This is slow and memory-heavy at the required 100,000 records. | Apply filter, sort, skip, and limit in MongoDB. |
| 21 | `list_attendance` | It calculated `total` after loading all matching documents. | It did not use a database count and was not scalable. | Use `count_documents(query)`. |
| 22 | `list_attendance` | It returned MongoDB `_id` indirectly as `id` and returned datetimes directly. | The response did not match the API schema. | Use `_record()` and epoch-millisecond conversion. |
| 23 | `list_attendance` | It did not validate dates, status values, or the date range. | Bad filters could be accepted instead of returning 422. | Added date parsing, enum constraints, and range validation. |
| 24 | Starter file | Six required endpoint groups were only TODOs. | Punch-out, regularization, analytics, and explain were missing. | Implemented the endpoints with MongoDB aggregation pipelines. |

## Additional issues found while testing the completed code

The review tests also found two issues in the first completed version:

1. MongoDB explain output contains BSON `Timestamp` values. Returning it
   directly caused HTTP 500. I converted the result to JSON-safe Extended
   JSON before returning it.
2. The trend pipeline begins with `$documents`. MongoDB requires the explain
   command to use the special `aggregate: 1` namespace for this pipeline. The
   trend explain branch now uses MongoDB's supported aggregate-explain form.

## Tests and evidence

The tests are in [tests/test_api.py](tests/test_api.py). They use the
configured MongoDB through the FastAPI application. Test-created records are
removed during teardown.

The suite covers:

* employee CRUD, filtering, pagination, and duplicate creation;
* timestamp and shift validation;
* the R2 late boundary and R3-R5 punch-out rules;
* an overnight shift;
* regularization history and legacy missing fields;
* no-log employees and mid-month working days;
* weekend and missing-day trend rows;
* leaderboard ties at the cutoff;
* concurrent punch-in and punch-out;
* all five explain targets and index plans.

Command:

```text
python -m unittest discover -s tests -v
```

Final result:

```text
Ran 12 tests in 3.137s
OK
```

The live MongoDB check also confirmed that the department summary changed from
`COLLSCAN` to `IXSCAN` after adding the `joined_on` index. Pylance reported no
diagnostics for `app/main.py`.

## Things checked and kept

* The unique indexes are intentional: they enforce the employee key and the
  one-record-per-employee-per-date rule.
* Analytics start from stored derived fields, as required by the data model.
  They do not recompute seeded values.
* The leaderboard uses competition ranking, so ties at the limit are kept.
* The trend uses `$documents`, `$unionWith`, `$densify`, and a window function
  because the contract requires database-side gap filling and moving averages.
* `history` is changed only by regularization. Punch-in and punch-out do not
  append audit entries.
