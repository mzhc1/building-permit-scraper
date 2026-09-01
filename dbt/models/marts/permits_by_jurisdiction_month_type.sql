-- Volume by jurisdiction / month / type. record_kind = 'permit' only --
-- Fee Estimates and Addenda are real Accela record types but not permits
-- (see stg_permits.sql), and counting them here would inflate volume with
-- records that aren't what this table claims to measure.

select
    jurisdiction,
    state,
    file_month,
    type,
    status,
    count(*) as permit_count,
    sum(case when residential then 1 else 0 end) as residential_count,
    sum(case when residential = false then 1 else 0 end) as commercial_count,
    sum(case when residential is null then 1 else 0 end) as residential_unknown_count
from {{ ref('stg_permits') }}
where record_kind = 'permit'
group by 1, 2, 3, 4, 5
