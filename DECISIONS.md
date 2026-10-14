# DECISIONS.md

## Indexes

I added indexes based on how the API queries the data. The employee code has a unique index, and attendance records have a unique index on `(emp_code, date)` so an employee cannot have multiple attendance records for the same day.

I also added indexes for date and employee lookups, status-based reports, and employee joining dates. The `joined_on` index is useful for the department summary, especially when no department filter is provided.

I avoided adding indexes to every field because they take up storage and add overhead when records are inserted or updated.

## Handling Concurrent Requests

Two punch-in requests for the same employee and date could arrive at almost the same time. Both might check for an existing record before either one is inserted.

Instead of relying only on the initial check, I use the unique attendance index to prevent duplicate records. If one request inserts successfully, the other gets a duplicate-key error, which the API handles by returning a `409 Conflict` response.

For punch-out, the update only succeeds when `punch_out` is still `null`. This prevents two requests from closing the same attendance record independently.

## Leaderboard Ranking

The late-arrival leaderboard uses competition ranking. Employees with the same number of late arrivals receive the same rank.

For example, if two employees share rank 3, the next employee receives rank 5. I apply the requested limit to the rank rather than simply cutting off the list by position, so employees tied at the cutoff are included.

## Department Summary

The department summary starts with employee records instead of attendance logs. This allows employees who have not recorded any attendance to be included in the headcount.

Starting from attendance records would exclude those employees and could produce an incorrect department total.

## Handling Larger Datasets

If the dataset grows to around 100 times its current size, I would consider storing pre-aggregated daily attendance statistics and using them for reporting endpoints.

This would reduce the amount of data processed for repeated reports. I would also check query execution plans and measure response times before deciding which additional indexes or optimizations are necessary. The API responses would remain unchanged so existing clients would continue to work.