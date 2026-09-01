-- Reported (Shovels API, via `probe`) vs actually scraped, per jurisdiction.
-- reported_permit_count comes from a stored seed observation
-- (seeds/reported_permit_counts.csv) -- a dated fact from a past probe run,
-- not a live API call. See that seed's `note` column for provenance and
-- staleness caveats before trusting the reported side of this table.

with scraped as (
    select
        jurisdiction,
        state,
        count(*) as scraped_permit_count
    from {{ ref('stg_permits') }}
    where record_kind = 'permit'
    group by 1, 2
)

select
    scraped.jurisdiction,
    scraped.state,
    scraped.scraped_permit_count,
    reported.reported_permit_count,
    reported.source as reported_source,
    reported.as_of_date as reported_as_of_date,
    reported.note as reported_note,
    case
        when reported.reported_permit_count is null then null
        else scraped.scraped_permit_count - reported.reported_permit_count
    end as coverage_gap
from scraped
left join {{ ref('reported_permit_counts') }} as reported
    on reported.jurisdiction = scraped.jurisdiction
    and reported.state = scraped.state
