-- Regression pin: record_kind's permit_number-prefix match originally used
-- 'EST-%'/'ADD-%' (with a literal hyphen before the digits), which matched
-- zero real rows -- Accela's actual prefixes are "EST25-0010", "ADD25-0137"
-- etc. with no hyphen before the year. That bug silently let Fee Estimates
-- and Addenda leak into permits_by_jurisdiction_month_type as if they were
-- real permits (caught by cross-checking against a stat the README already
-- documents: 24 Fee Estimates, 174 Addenda in the Paso Robles Building
-- module -- same "verify against a number you didn't just compute" habit
-- as PaginationMismatch in src/adapters/accela.py).
--
-- Written as explicit scalar counts, not a GROUP BY ... HAVING, on purpose:
-- with the original bug, record_kind never took the value 'fee_estimate'
-- or 'addendum' at all, so those groups simply didn't exist and a HAVING
-- check on them silently passed. A dbt singular test fails if this query
-- returns any rows.

select 'fee_estimate count mismatch' as failure_reason
where (
    select count(*) from {{ ref('stg_permits') }}
    where jurisdiction = 'Paso Robles' and record_kind = 'fee_estimate'
) != 24

union all

select 'addendum count mismatch'
where (
    select count(*) from {{ ref('stg_permits') }}
    where jurisdiction = 'Paso Robles' and record_kind = 'addendum'
) != 174
