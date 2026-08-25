# Listing due-diligence snapshots

The detail pane can hold a sourced, dated assessment of a listing's market
value, VvE, erfpacht, and property-specific risks. These snapshots are decision
support, not appraisals or substitutes for the underlying documents.

## Review method

### 1. Establish the live facts

- Confirm that the listing is still available and record its current asking
  price, area, asking price per square metre, time on market, and price history.
- Download and inspect the listing brochure when one is available. Brochure
  floor plans, measurement notes, clauses, and VvE/erfpacht wording can contain
  material facts that are absent from the property-page summary.
- Read the complete description and characteristics. Do not infer VvE or
  erfpacht health from a short search-result snippet.
- Note inconsistencies between the description, Funda checklist, cadastral
  record, measurement report, and third-party sources.

### 2. Review the VvE

Extract the monthly contribution, registration, annual meetings, reserve fund,
MJOP, building insurance, professional management, and recent major work. Then
ask what the checkboxes do not answer:

- Is the reserve large enough for the MJOP, and what is this apartment's share?
- Do the latest accounts and meeting minutes mention special assessments,
  arrears, disputes, foundation work, roof/facade work, lifts, or sustainability
  projects?
- Is the stated contribution established or merely provisional?
- For a new subdivision, are the deed, opening balance, budget, insurance, and
  management arrangements final?

Risk is `low` only when the published facts are internally consistent and show
an operating VvE with a reserve and maintenance plan. Missing documents produce
`unknown`; contradictions, a new subdivision, or unfunded work produce
`medium` or `high` depending on likely financing and cash impact.

### 3. Review erfpacht

Classify the property as one of:

- own ground;
- current period paid off, with the future arrangement unknown;
- current period paid off and future annual canon fixed;
- annual canon payable and indexed;
- perpetual erfpacht notarized and paid off.

Record the current canon, end date, indexation, applicable terms, and future
canon or buyout. An application or accepted offer is not treated as completed
until the notarial deed is confirmed. Missing post-period terms are a risk even
when the current period is paid off.

### 4. Identify property and financing flags

Look specifically for unfinished subdivision, discrepancies in residential
area, souterrain use, permissions for roof terraces or extensions, monuments,
foundation indicators, non-owner-occupancy and age clauses, overdue
modernization, old installations, rights of way, and unusually high recurring
costs. State why each item matters: valuation, mortgage eligibility, immediate
cash, or living quality.

### 5. Estimate market value

- Start with the current ask and price per square metre.
- Compare the historic Amsterdam transaction band, current WOZ, and preferably
  an address-specific external model such as Woningstats, Huispedia, or Walter.
- Check whether those sources use the same area and property type.
- Adjust the interpretation for condition, lease costs, time on market, legal
  uncertainty, and features the model misses.
- Publish a range and confidence level. Preserve the external model separately
  so its number is not confused with the reviewed indication.

The result must say when a model is stale, condition-blind, based on a different
area, or otherwise unsuitable as an appraisal proxy.

### 6. Leave actionable questions and sources

Every snapshot ends with the questions that should be answered before viewing
or bidding and direct links to the listing and valuation sources. Date the
snapshot so it can be refreshed when the listing or documents change.

## Request workflow

Open a listing's detail pane and choose **Request analysis**. The server stores
the listing ID, request time, property URL, and discovered brochure PDF URL in
`data/analysis_requests.json`. Completed
snapshots live in `data/listing_analyses.json`; saving one clears its outstanding
request and retains the brochure URL. An existing snapshot has **Request
refresh** for changed listings or new documents.

Requests deliberately enter a review queue instead of invoking an unattended
LLM. The review depends on live sources, document interpretation, and explicit
uncertainty. Process queued requests with Codex, which can research the listing
and save the structured result through `POST /analysis`.

## Stored shape

```json
{
  "market": {
    "estimate_low": 700000,
    "estimate_high": 750000,
    "confidence": "medium",
    "summary": "Reviewed interpretation of the range.",
    "external": {
      "label": "Woningstats",
      "value": 742000,
      "url": "https://example.com/value",
      "caveat": "Condition-blind model."
    }
  },
  "vve": {
    "risk": "medium",
    "monthly_eur": 250,
    "summary": "Published facts and missing evidence."
  },
  "erfpacht": {
    "risk": "low",
    "headline": "Bought out perpetually",
    "summary": "Published terms and verification needed."
  },
  "flags": ["What materially jumps out."],
  "questions": ["What should be answered before bidding?"],
  "sources": [{"label": "Funda", "url": "https://example.com/listing"}]
}
```
