# DECISIONS.md

1. **Indexes.**
On `employees`, we created a unique index on `emp_code` to guarantee identifier uniqueness, and compound indexes on `(department, joined_on)` and `(joined_on, department)` to support department lookups and headcount date filtering. On `attendance_logs`, we created a unique index on `(emp_code, date)` to enforce one record per day and guarantee punch-in race safety, `(date: -1, emp_code: 1)` to back paginated attendance queries, `(emp_code: 1, punch_in: -1)` for fast punch-out lookups, and `(date: 1, late_minutes: 1)` for leaderboard scans. We considered and rejected indexing `status` due to its low cardinality, which provides poor index selectivity.

2. **Punch-in race.**
Both concurrent requests calculate the attendance date (R1) and attempt `insert_one`. MongoDB's WiredTiger engine serializes the index insertion against the unique `(emp_code, date)` index. Exactly one request succeeds with HTTP 201; the second fails with `DuplicateKeyError`, which our exception handler intercepts to immediately return HTTP 409 without race windows.

3. **Ties.**
The pipeline applies `$setWindowFields` with `$rank` for competition ranking (1, 2, 2, 4). Because `limit` filters on `rank <= limit` *after* ranking, both tied employees qualify and are returned, ordered by `total_late_minutes` descending and `emp_code` ascending.

4. **Headcount.**
The pipeline drives from `employees` (matching `joined_on <= month_end`) and left-joins `attendance_logs` using `$lookup`. Unwinding with `preserveNullAndEmptyArrays: true` ensures employees with zero attendance records are preserved and counted in `$addToSet: "$emp_code"`.

5. **One thing you would change for 100x the data.**
I would maintain pre-aggregated daily departmental rollups via background triggers and shard `attendance_logs` on a compound shard key `(date, emp_code)` to prevent scanning raw logs for monthly analytics across millions of records.
