# DECISIONS.md
1. __Indexes:__ I created unique indexes on `employees.emp_code` and `attendance_logs.(emp_code, date)`, plus supporting compound indexes for department queries and the attendance list sort. These guarantee uniqueness under concurrent creates and allow fast filtered lookups. I rejected a broader `all fields` index because it would add write cost without helping the contract queries.

2. __Punch-in race:__ Two identical requests arrive together. The application finds or creates a record by `emp_code` + `date`, and the database unique index rejects the second insert with a duplicate-key error. The API turns that into a 409, so only one request wins and the other is safely rejected instead of creating two attendance rows.

3. __Ties:__ The leaderboard ranks by total late minutes and then by `emp_code` ascending. It uses standard competition ranking, so tied totals share the same rank and the next rank is skipped. The endpoint applies `limit` after ranking, so employees tied at the cutoff are included as long as their rank is within the limit.

4. __Headcount:__ The department summary first filters employees by `joined_on <= month_end` and then keeps every employee in the department, even when they have no attendance records. That makes zero-log employees count in headcount while still excluding employees hired after the period closes.

6. One thing I would change if this would have to handle 100x the data is Sharding and Indexing along with Pre-aggregation for materializing reporting layer for monthly trend queries because MongoDB aggression over very large collections would become the bottleneck. Indexing on departments and date definitley helps for heavy queries.