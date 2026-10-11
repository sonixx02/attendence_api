# DECISIONS.md

I create a unique index on `employees.emp_code` because it enforces the
business identifier and makes duplicate employee creation race-safe. I create
a unique compound index on `attendance_logs.(emp_code, date)` for the natural
attendance key, plus indexes supporting date/employee listing and status/date
analytics. I considered an index on every individual field, but rejected that
because it adds write cost without matching a query shape.

For two simultaneous punch-ins, both requests may initially see no record, but
only one insert can satisfy the unique `(emp_code, date)` index. That request
returns 201; the other receives `DuplicateKeyError` and returns 409. No
check-then-insert decision is trusted.

The leaderboard uses MongoDB competition ranking. If two employees tie at rank
10 and the limit is 10, both rows are returned, because the limit is applied
to the rank rather than the array position.

Department summaries start from employees, filter by `joined_on <= period_end`,
and then look up logs. Employees with no logs therefore still contribute to
headcount.

At 100x the data, I would move heavy reports to pre-aggregated daily
collections or scheduled materialized summaries while keeping the API contract
unchanged.
