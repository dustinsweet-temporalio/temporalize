# Discovery sheet: demo

Questions that turn assumptions into facts, most valuable first, grouped by who to ask.

## Ask Operations

1. How many times a month does a failure leave a duplicate, missed or half-done side effect?
   - Why: Needed to estimate: duplicate or unrecovered side effects. (cost input: incidents per month (incidents/month); for Order payment)

## Ask Finance

1. What does one duplicate or missed side effect cost (refunds, credits, penalties, staff time)?
   - Why: Needed to estimate: duplicate or unrecovered side effects. (cost input: value per incident (currency/incident); for Order payment)

## Ask Support

1. How do you find orders that were charged but never shipped?
   - Why: Shows how long the problem goes unnoticed. (resolves: detection delay; for Order payment, Nightly export)

## Missing inputs for cost estimates

- Order payment: incidents per month
- Order payment: value per incident

## Assumptions

These are not established by the code or the customer context:

- Order payment: Support staff check payments against shipments by hand.
- Order payment: How often the server restarts mid-order.
- Order payment: Assumes 1,000 orders a day.
- Nightly export: Support staff check payments against shipments by hand.
- Nightly export: How often the server restarts mid-order.
- Nightly export: Assumes 1,000 orders a day.

## Customer context used

None given. The briefs are qualitative only.


---

_Built from the Temporal report of commit abc1234 (generated 2026-10-20T10:00:00+00:00); mapping business-impact-map 1.0.0; model fake-model; no customer context. Numbers are illustrative, computed from stated inputs._
