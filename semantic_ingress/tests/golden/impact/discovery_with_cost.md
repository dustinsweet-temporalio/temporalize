# Discovery sheet: demo

Questions that turn assumptions into facts, most valuable first, grouped by who to ask.

## Ask Support

1. How do you find orders that were charged but never shipped?
   - Why: Shows how long the problem goes unnoticed. (resolves: detection delay; for Order payment, Nightly export)

## Assumptions

These are not established by the code or the customer context:

- Order payment: Support staff check payments against shipments by hand.
- Order payment: How often the server restarts mid-order.
- Order payment: share unrecovered 0.1-0.5 fraction (default)
- Order payment: Assumes 1,000 orders a day.
- Nightly export: Support staff check payments against shipments by hand.
- Nightly export: How often the server restarts mid-order.
- Nightly export: Assumes 1,000 orders a day.

## Customer context used

- C1: Hourly rate: 90 USD/hour
- C2: Industry: Mid-market fintech
- C3: Notes: About 4 failed payout batches a month.
- C4: Notes: Finance reruns failed batches by hand, about 3 hours each.
- C5: Notes: Each missed payout costs around 250 in credits.


---

_Built from the Temporal report of commit abc1234 (generated 2026-10-20T10:00:00+00:00); mapping business-impact-map 1.0.0; model fake-model; context 2b7e954e22b5e026. Numbers are illustrative, computed from stated inputs._
