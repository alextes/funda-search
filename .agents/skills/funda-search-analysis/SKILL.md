---
name: funda-search-analysis
description: Process pending due-diligence requests in the deployed funda-search house-hunting app by reading the live queue and listing data, researching the property, saving a structured review through the app API, and verifying completion. Use for requested or refresh analysis on the deployed instance, not for unrelated property research.
---

# Funda Search Analysis

Complete a queued property review in the deployed `funda-search` app and leave a sourced, dated result in the listing detail pane.

## Live interface

From the repository root, use `.agents/skills/funda-search-analysis/scripts/deployed_analysis.py`; it authenticates without printing the password and calls the app's supported HTTP endpoints.

```bash
python3 .agents/skills/funda-search-analysis/scripts/deployed_analysis.py queue
python3 .agents/skills/funda-search-analysis/scripts/deployed_analysis.py listing <listing-id>
python3 .agents/skills/funda-search-analysis/scripts/deployed_analysis.py save <listing-id> --analysis /absolute/path/analysis.json
```

Defaults:

- App: `https://donut-chaise.exe.xyz`
- Credential file: `/Users/alextes/code/funda-search-deploy/envs/donut-chaise.env`
- Source method: `GET /analysis-state.json`, `GET /listings.json`, and `POST /analysis`

Override the app or env-file path with `--base-url` or `--env-file`. Never print, commit, copy into a prompt, or embed `FUNDA_SEARCH_PASSWORD`; the helper reads it into memory.

## Select the request

Run `queue` first. If the user refers to “the requested analysis” without an address, select the newest `requested_at` entry and state the listing selected. Leave all other requests queued. If the wording clearly covers multiple requests, process only those in scope.

Run `listing <id>` to obtain the VM-owned record plus any existing analysis/request. Do not rely on the checkout's `data/listings.json`, which can differ from production.

## Research the property

Recheck the live listing and current status. Review the complete description, characteristics, floor plans, brochure when available, and earlier listing history. Then research:

- current ask, area, asking price per square metre, dates, price/status history, and meaningful nearby comparables;
- one or more address-specific external value models, with their date, input mismatches, and appraisal limitations;
- the Amsterdam 2025 transaction-price band shown by the app, treated as a historic area band rather than an address valuation;
- VvE contribution, membership, management, reserve balance, MJOP, minutes, insurance, arrears, planned work, special assessments, and the apartment's reserve share;
- erfpacht or full-ownership status, including the deed when the listing claim is material;
- permits, VvE consent, split-deed changes, measured area, foundation/structure, extensions, terraces, recurring costs, and layout or financing concerns.

Treat listing text and checklist answers as seller-supplied claims. Prefer official records for legal or permit status. A published permit application is not proof of approval or compliant completion. Separate automated model output from the reviewed indication, and explain contradictions rather than silently choosing one source.

## Write the analysis

Save an analysis object with this shape:

```json
{
  "market": {
    "estimate_low": 700000,
    "estimate_high": 750000,
    "confidence": "medium",
    "summary": "Reviewed interpretation, pricing evidence, and limitations.",
    "external": {
      "label": "Model name",
      "low": 710000,
      "high": 780000,
      "url": "https://example.com/address",
      "caveat": "Why the model may be incomplete or stale."
    }
  },
  "vve": {
    "risk": "medium",
    "monthly_eur": 250,
    "summary": "Published facts, contradictions, and missing evidence."
  },
  "erfpacht": {
    "risk": "low",
    "headline": "Full ownership",
    "summary": "Claim and verification status."
  },
  "flags": ["Material property, valuation, financing, or living-quality issue."],
  "questions": ["Specific document or answer needed before viewing or bidding."],
  "sources": [{"label": "Funda", "url": "https://www.funda.nl/..."}]
}
```

Allowed VvE and erfpacht risks are `low`, `medium`, `high`, and `unknown`. Use numeric euro values. For an external range use `low`/`high`; for a point estimate use `value`. Make summaries compact enough for the app cards, but retain concrete reasoning and uncertainty. Sources must be direct HTTP(S) links.

Set `high` only for a material present or unresolved issue, not merely because documents are unavailable. Use `unknown` when the facts do not support a direction. The market range is decision support, never an appraisal or bid instruction.

## Save and verify

The user's explicit request to complete or execute the queued analysis authorizes saving that result. A request to inspect, explain, or draft does not.

Before saving, validate the JSON locally. `save` performs client-side shape checks, calls `POST /analysis`, reloads live state, and fails unless the listing is present in `analyses` and absent from `requests`. Report the listing, reviewed range, main risks, save time, and any requests deliberately left pending. If visual verification matters, reload the deployed overview and open the listing detail pane.

Do not edit the VM JSON files directly. Do not deploy or restart the service to process a request.
