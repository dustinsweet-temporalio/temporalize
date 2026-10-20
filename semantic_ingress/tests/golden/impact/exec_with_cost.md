# Business impact: demo

**Failed orders can charge customers without shipping their goods.**

Order payment carries the strongest case. Report email is not worth pursuing.

## Where failures would cost the business

### 1. Order payment: Strong case

- **Duplicate or missed side effect** (high impact; felt by Customers, Support): If the server restarts after the card is charged, the order is never shipped and the customer has paid for nothing.

Why it matters: Customers pay without receiving goods, which creates refunds and support work.

**Illustrative cost: 100 USD to 500 USD per month**

**Duplicate or unrecovered side effects** = `incidents_per_month * value_per_incident * share_unrecovered`

| Input | Value | Unit | Source |
|---|---|---|---|
| incidents per month | 4 | incidents/month | context C3 |
| value per incident | 250 | currency/incident | context C5 |
| share unrecovered | 0.1 to 0.5 | fraction | default (placeholder) |

_Illustrative estimate from the stated inputs. Not a prediction._

### 2. Nightly export: Moderate case

- **Missed or overlapping scheduled runs** (medium impact; felt by Teams that use the data): If the server restarts during the nightly export, the partial file is lost and the export starts again the next night.

Why it matters: Customers pay without receiving goods, which creates refunds and support work.

_Capped at moderate: the Temporal report rates the process borderline and some facts about the process are low confidence._

## Not worth pursuing

- **Report email**: It is a single fast database read.
  - Would change if: It started calling paid services.

## No failure story from the code

The Temporal report rates these processes a fit, but their code facts show nothing a crash would lose, so there is no business story to tell yet:

- **Lookup**

## Recommended first step

Confirm the inputs behind the Order payment estimate with the customer, then scope a proof of concept on Order payment.

## Assumptions

These are not established by the code or the customer context:

- Order payment: Support staff check payments against shipments by hand.
- Order payment: How often the server restarts mid-order.
- Order payment: share unrecovered 0.1-0.5 fraction (default)
- Order payment: Assumes 1,000 orders a day.
- Nightly export: Support staff check payments against shipments by hand.
- Nightly export: How often the server restarts mid-order.
- Nightly export: Assumes 1,000 orders a day.


---

_Built from the Temporal report of commit abc1234 (generated 2026-10-20T10:00:00+00:00); mapping business-impact-map 1.0.0; model fake-model; context 2b7e954e22b5e026. Numbers are illustrative, computed from stated inputs._
