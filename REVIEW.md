# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:


| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `app/main.py` – `compute_late_minutes`, `compute_work_hours`, `compute_overtime` | The helper functions used naive timestamps and did not match the business rules for IST time zones, overnight shifts, late grace windows, or half-day/overtime rounding. | A punch-in after 09:30 on a night shift or a short overtime record produced wrong minutes/hours, and some values were always zero or always positive regardless of rules. | In `app/mainC.py`, the helper section was rewritten to use `truncate_to_seconds()`, `attendance_date()`, IST-aware datetime comparison, decimal rounding, and overnight shift checks. |
| 2 | `app/main.py` – `list_employees` and `list_attendance` | Pagination was off by one and there was no contract-driven validation for `page`, `page_size`, date filters, or status values. | Requests like `page=2&page_size=20` skipped 40 records instead of 20, and invalid query values were silently accepted. | `app/mainC.py` adds `build_attendance_query()` plus strict Query constraints and correct offset math before fetching records. |
| 3 | `app/main.py` – `PunchInIn` model and `punch_in` endpoint | Employee existence was not verified, invalid statuses were accepted, duplicate records could be created incorrectly, and the attendance date ignored IST/overnight behavior. | Punching in for a missing employee or a duplicate date could succeed, or the record date was computed against local time instead of the shift date. | The completed implementation validates `PresenceStatus`, checks the employee record, uses `attendance_date()`, and prevents duplicate entries through Mongo uniqueness rules and `record_out()` normalization. |
| 4 | `app/main.py` – `EmployeeIn` model | The schema did not enforce the API contract for employee codes, email format, joined dates, or time values. | Invalid payloads like `EMP-1`, malformed emails, or `joined_on` values not in `YYYY-MM-DD` were accepted. | `EmployeeIn`, `PunchInIn`, `PunchOutIn`, and `RegularizeIn` in `app/mainC.py` use `Field(...)` constraints and `model_validator()` checks. |
| 5 | `app/main.py` – TODO block below the attendance endpoints | The contract-required punch-out, regularization, analytics, and admin explain endpoints were missing entirely. | Calling `/attendance/punch-out`, `/attendance/{emp_code}/{date}`, `/analytics/...`, or `/admin/explain/{endpoint}` returned not found or was left as a stub. | `app/mainC.py` implements all missing endpoints, including a `regularize_attendance()` flow, Mongo aggregation pipelines, and the explain-plan endpoint. |
| 6 | `app/main.py` – startup / Mongo config | There were no startup indexes or a health check to validate Mongo connectivity, so the database was vulnerable to duplicate or inconsistent records. | Duplicate employee codes or attendance logs were not blocked; a database outage still looked healthy to the app. | `ensure_indexes()` and the `lifespan` startup hook were added in `app/mainC.py`, and the health endpoint checks DB availability rather than returning a constant success message. |

Also noted as not a defect:

- The basic `GET /health` route was a harmless smoke-check and did not need major rewriting beyond making it database-aware.
- The happy-path employee creation and attendance listing flow was already structurally sound enough to keep, so the changes were focused on correctness, validation, and contract completion rather than rebuilding the whole app.
- The Mongo-backed data model itself was fine; the real defects were in validation, business rules, missing endpoints, and lack of DB safeguards.

