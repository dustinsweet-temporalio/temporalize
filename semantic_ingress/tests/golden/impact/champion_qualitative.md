# Business impact for engineering: demo

Failed orders can charge customers without shipping their goods.

## Order payment

Strong case. Temporal report: suitable, 7.2/10; strongest reason: Distributed transaction. Entry point: `order_payment.py`.

### Failure stories

- **Duplicate or missed side effect** (high impact, stage: after charging): If the server restarts after the card is charged, the order is never shipped and the customer has paid for nothing.
  - F7 crash after charging: The shipment; the charge is not undone. [orders/flow.py@L3-4]
  - F3 step 1: charge: Charges the card. (paid_call) [orders/pay.py@L1-2]

### What a crash loses today

- **after charging**: The shipment; the charge is not undone. [orders/flow.py@L3-4]

### What changes with Temporal

**Gains:**
- Charges are never repeated (F3, F7)

**Removes:**
- orders/flow.py: retry loop

**Stays outside Temporal:**
- The web form

**Workaround today** (assumption): Support staff check payments against shipments by hand.

**What decides the size of the problem:**
- frequency (assumption): How often the server restarts mid-order.
- blast radius (code): Each crash affects the one order in flight.

### Verdict

Suitable: a distributed transaction.

### Proof-of-concept scope

Start at `order_payment.py`. Show: Charges are never repeated.

## Nightly export

Moderate case. Temporal report: borderline, 5.4/10; strongest reason: Scheduled work. Entry point: `nightly_export.py`.

_Capped at moderate: the Temporal report rates the process borderline and some facts about the process are low confidence._

> **Low confidence:** F6 unsupported

### Failure stories

- **Missed or overlapping scheduled runs** (medium impact, stage: while exporting): If the server restarts during the nightly export, the partial file is lost and the export starts again the next night.
  - F1 trigger: A cron entry at midnight. [orders/flow.py@L1-2]
  - F6 crash while exporting: The partial export; the next night starts over. [jobs/export.py@L5]

### What a crash loses today

- **while exporting**: The partial export; the next night starts over. [jobs/export.py@L5]

### What changes with Temporal

**Gains:**
- Charges are never repeated (F3, F7)

**Removes:**
- orders/flow.py: retry loop

**Stays outside Temporal:**
- The web form

**Workaround today** (assumption): Support staff check payments against shipments by hand.

**What decides the size of the problem:**
- frequency (assumption): How often the server restarts mid-order.
- blast radius (code): Each crash affects the one order in flight.

### Verdict

Suitable: a distributed transaction.

### Proof-of-concept scope

Start at `nightly_export.py`. Show: Charges are never repeated.

## Not worth pursuing

- **Report email**: It is a single fast database read.
  - Would change if: It started calling paid services.

## No failure story from the code

The Temporal report rates these processes a fit, but their code facts show nothing a crash would lose, so there is no business story to tell yet:

- **Lookup**

## Not assessed

- **Broken**: The Temporal report could not assess it: investigation timed out


---

_Built from the Temporal report of commit abc1234 (generated 2026-10-20T10:00:00+00:00); mapping business-impact-map 1.0.0; model fake-model; no customer context. Numbers are illustrative, computed from stated inputs._
