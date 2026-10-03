# Pricing, checkout and allowances

The decision record is ADR-A59 (`docs/architecture/ADR.md`); payment gateways
are ADR-A58. This page is the operating reference: what each plan costs and
allows, how a paid plan starts, and where each rule lives in code. Tracker rows
QU-1 to QU-6 and PY-1 to PY-6.

## Plans

| Plan | Audience | Price | Set in |
|---|---|---|---|
| Free | candidate | NPR 0 | `apps/billing/seeds.py` |
| Pro | candidate | **NPR 1,700 a month** (`PRO_PRICE_NPR`) | `apps/billing/seeds.py` |
| Employer | company | quoted ("Talk to us"); no online checkout | `apps/billing/seeds.py` |

- Prices are in NPR because eSewa and Khalti settle only in NPR. Pro was
  USD 12; at the stored rate of 141.5 that is NPR 1,698, rounded to 1,700.
- To change a price or an allowance: edit `seeds.py`, then
  `python manage.py seed_plans`. Nothing else holds a number.
- Every price on screen goes through `billing.money.format_money` (template
  filter `money`), e.g. "NPR 1,700". Pricing page, plan page, checkout
  panel, invoices, payment pages and the FAQ all read the same plan row.
- Old invoices keep the currency they were issued in.

## Allowances

| Allowance (limit key) | Period | Free | Pro | Claimed by |
|---|---|---|---|---|
| CV analyses (`resume_analyses`) | month | 1 | unlimited | `analysis.analyse` |
| Assisted CV replies (`assistant_replies`) | day | 20 | 300 | `assistant.respond` |
| CV improvement drafts (`cv_improvements`) | day | 5 | 100 | `improvements.submit` (new prepare jobs), `analysis.apply_finding` (AI drafts) |
| Practice interviews (`interview_practice`) | month | 2 | 40 | `interviews.engine.start` (practice only) |
| Cover letter drafts (`cover_letters`) | month | 2 | 50 | `letters.draft` |

- Registry: `apps/billing/limits.py` (key, kind, period, refusal text,
  audience). Numbers live only in the plan rows.
- A day is the calendar day, a month the calendar month, in the site's time
  zone (`UsageCounter.start_of`).
- **Enforced on the server.** `entitlements.claim` increments the counter
  atomically (`usage.take`, a conditional `UPDATE`), so two tabs or a double
  click cannot both take the last unit.
- **No charge for work that never happened.** `entitlements.release` is
  called when the model run fails, a draft comes back empty, a job stalls
  (`fail_stalled`) or an interview cannot be planned.
- **Not charged twice.** A resubmitted form or a second request for a job that
  is already running reuses the existing job without claiming.
- **Employer-invited interviews** are never metered.
- **At the limit** the request is refused with HTTP 402 and a normal product
  message, titled "You've reached your plan's limit": what was used, the
  limit, the reset date (`entitlements.resets_on`) and links to the plan page
  and pricing. The assistant writes the same message into the conversation
  instead of calling the model, keeping what the candidate typed.
- **Shown before the limit.** The plan page lists every allowance with "left
  today" / "left this month" and "Resets on"; the practice interview hub says
  how many are left this month. The pricing cards list the allowances from the
  same plan rows (`selectors._allowance_lines`), so the page cannot advertise
  a number the code does not enforce.

## Account states

| State | Allowances used | Notes |
|---|---|---|
| Free (no subscription) | Free plan | default plan |
| Pro, active | Pro plan | |
| Pro, `incomplete` (awaiting payment) | **Free plan** | a checkout confers nothing |
| Pro, cancelled | Pro until the period ends, then Free | downgrade waits for period end |
| Pro, `past_due` (renewal unpaid) | Pro during the grace period (`GRACE_DAYS` = 14) | `expire_overdue` then moves it to Free |
| Pro, expired | Free plan | |

## Checkout (candidate)

1. **Pricing → Start with Pro** or **Plan → Upgrade** calls
   `subscriptions.start_checkout`: an `incomplete` subscription and an open
   invoice for NPR 1,700. The candidate stays on Free.
2. The plan page shows the checkout panel: amount, "for one month of Pro",
   the payment buttons (eSewa, and Khalti when configured), the bank-transfer
   note and **Don't start Pro** (abandon, with confirmation).
3. The candidate pays on the provider's page.
4. The return is **verified server-side** with the provider (ADR-A58) before
   anything changes. `payments._record` then calls `subscriptions.activate_paid`:
   the subscription becomes active from the payment date, the previous plan
   ends, and the invoice is marked paid in the same transaction.
5. A callback that never arrives is caught by `reconcile_payments` (every
   5 minutes), which asks the provider again.

Rules:
- One open checkout per owner (partial unique constraints
  `subscription_one_checkout_per_candidate` / `_per_company`). Choosing again
  returns the same checkout.
- A checkout with a payment attempt still pending at the provider cannot be
  replaced.
- Unpaid checkouts expire after 7 days (`expire_checkouts`, nightly at 02:50).
- A payment that arrives after expiry is recorded, flagged for refund, and
  grants nothing.
- Bank transfers are recorded by staff and follow the same `_record` path.

## Tests

- `test_checkout_and_quotas.py`: checkout creation, idempotency, abandon,
  activation only on verified payment, expiry, late payment, each allowance,
  concurrency, refunds on failure, reset dates, the refusal text.
- `test_billing_decisions.py`: ADR-A59 decisions (supersedes D-08's "at
  once").
- `test_subscriptions.py`, `test_renewal_concurrency.py`, `test_tasks.py`:
  renewal, cancellation, period end, grace and overdue expiry, reactivation.
- `test_gateways.py`: eSewa and Khalti verification, tampered and replayed
  returns, amount mismatch.
- `test_entitlements.py`, `test_claim.py`, `test_enforcement.py`: the claim
  machinery.
- Browser: Journey C (two practice interviews, the third refused with the
  reset date), Journey D (NPR 1,700 everywhere, checkout panel, hand-off to the
  eSewa sandbox showing 1,700, cancel at eSewa returns with the account still
  on Free).

## Needs a person

- Completing an eSewa sandbox payment: the eSewa login has a reCAPTCHA, which
  automation must not solve. Test wallet details are on eSewa's developer page.
- Khalti: a merchant sandbox secret key from test-admin.khalti.com in
  `KHALTI_SECRET_KEY`.
