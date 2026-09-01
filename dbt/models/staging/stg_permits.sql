-- Typed, cleaned pass over the raw loaded table. Does not reinterpret any
-- field the scraper normalized in src/schema.py -- the one thing this layer
-- adds is record_kind, because the Accela Building module returns Fee
-- Estimates and Addenda in the same grid as real permits (see README:
-- "The Building module returns Fee Estimates and Addenda alongside real
-- permits"). That's read directly off the permit_number prefix Accela
-- itself uses -- confirmed against real Paso Robles data as "EST25-0010",
-- "ADD25-0137" etc. (no hyphen between the letters and the year, unlike
-- the README's "EST-"/"ADD-" shorthand), not inferred from free text.

select
    record_id,
    permit_number,
    jurisdiction,
    state,
    file_date,
    issue_date,
    final_date,
    status,
    type,
    subtype,
    description,
    job_value,
    residential,
    owner_name,
    contractor_name,
    contractor_license,
    street_no,
    street,
    city,
    zipcode,
    source_url,
    scraped_at,
    loaded_at,
    date_trunc('month', file_date) as file_month,
    case
        when permit_number ilike 'EST%' then 'fee_estimate'
        when permit_number ilike 'ADD%' then 'addendum'
        else 'permit'
    end as record_kind
from {{ source('raw', 'permits') }}
