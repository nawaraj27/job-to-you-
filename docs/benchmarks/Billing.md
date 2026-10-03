# Plans and Billing — Section Loop Report

**Date:** 2026-09-15.
**Scope:** SRS §5.14 (plans, subscriptions, invoices, payments, coupons, usage), the §19 billing state machine (`active → past_due → cancelled/expired`) and its nightly tasks, P18 entitlements, the candidate plan screen, the company billing screen and the public pricing page.

**Local status: STABLE. Not READY.** Manual-only checks: [`Manual-Verification-Consolidated.md`](Manual-Verification-Consolidated.md) rows 1.3k, 1.3l (Blocked: real gateway) and 2.5h. Real payment gateways (eSewa, Khalti, Fonepay) are deferred by SRS §21, and nothing here was tested against one.

**Why this section came next:** it is the last unlooped section that moves money and decides what somebody is entitled to. Getting its states wrong either gives a paid feature away indefinitely or takes away something already paid for.

## Method

- **Journey probe on the development database** (`billing_probe.py`, scratchpad, rolled back):
  - an unpaid Pro subscription driven through the four nightly tasks, one simulated day at a time, for 200 days;
  - paying the newer of two invoices while the older is overdue;
  - one coupon used on two invoices;
  - a gateway callback in another currency;
  - an employer billing post with an unknown action;
  - two policy observations: self-serve Pro before payment, and a mid-period change of plan.
- **Real app:** the demo candidate's plan screen (Pro, open invoice), before and after the fixes.
- **Browser scan:** `scripts/billing_a11y.py` covers the candidate plan screen, the pricing page and the company billing screen. It runs axe in light and dark at 1280 px and checks overflow at 375 and 320 px (12 scans).

## Defects found and fixed

| # | Defect | Severity | Root cause | Fix | Regression test |
|---|---|---|---|---|---|
| B1 | **A subscription that never paid never ended.** In the probe, an unpaid Pro subscription was still `past_due` after 200 nights with **7 open invoices**, keeping Pro the whole time (SRS §5.14: withdrawal after fourteen days) | High (paid plan given away indefinitely; unpaid invoices piling up) | `renew_due` renewed `past_due` subscriptions, and `renew` set them `active`. `end_unpaid` measures grace from `current_period_end`, which renewal kept moving forward | Renewal skips `past_due` subscriptions and no longer clears that state (only `trialing` becomes `active`). The probe case now ends on day 46 (period end plus the 14-day grace), with one invoice | `UnpaidLifecycleTests` (4, including the day-by-day run) |
| B2 | **Paying this month's invoice cleared `past_due` while last month's was still unpaid**, restarting the grace on a remaining debt | Medium | `_reactivate` looked only at the invoice just paid | Reactivates only when no other open invoice for the subscription is overdue | `test_paying_the_newer_invoice_leaves_an_older_overdue_one_past_due` |
| B3 | **A payment in another currency settled an invoice.** A USD 12.00 invoice was marked paid by NPR 12.00, and the gateway signature did not cover the currency (the test gateway sent none and NPR was assumed) | High (money) | `_record` compared amounts only; `fake.sign` covered reference, invoice and amount | Settlement requires the same currency. The test gateway reports the invoice currency, signs it, and has no default | `CurrencyTests` (3) |
| B4 | **Two invoices raised with one coupon counted one redemption**, so a capped coupon could be used past its cap | Medium | `invoices.issue` wrote `coupon.redemptions + 1` from the object in hand (`coupons.redeem` already used `F()`) | Through `coupons.redeem`, and only when the coupon actually took something off | `CouponCountTests` (2) |
| B5 | **Any company billing post other than "cancel" resumed a cancelled plan** | Medium | The view's `else` branch was "resume" | Explicit `resume`; anything else is refused with a message | `EmployerBillingActionTests` (2) |
| B6 | Usage panel: the candidate saw **"Open postings — Unlimited on this plan"** and an employer saw "CV analyses" | Low (misleading) | Every metered limit was listed for every owner | `Limit.audience` (`resume_analyses` candidate, `active_jobs` company); `usage_summary` filters on it | `UsagePanelAudienceTests` (2) |
| B7 | Both plan screens: axe **serious** `definition-list` / `dlitem` | Serious (accessibility) | Each `dt`/`dd` sat two `div`s under the `dl`, and the empty message was a `<p>` inside it | One `div.card.card-body` per group directly under the `dl`; the empty state is outside the list | `UsagePanelMarkupTests` (2); axe rescan |

The candidate plan view's docstring said a Pro subscription "is started by staff or by paying an invoice". The code starts it at once. The docstring now states what happens and points to the open decision below.

## Verified working without change

- One live subscription per owner (service check plus partial unique constraints). A double-click on "Move to Pro" is refused with a sentence, not a 500.
- One invoice per (subscription, period): the constraint decides the race, and a double renewal bills once.
- A replayed gateway callback records one payment. An unsigned or amount-edited callback writes nothing. An underpayment leaves the invoice open.
- Cancelling keeps the plan to the period end, resuming clears the date, and a cancelled-and-overdue subscription ends as a cancellation.
- A recruiter or company admin can read the company billing screen, but only `billing.manage` (owner) can change it. A stranger gets 404. Another company's or candidate's invoices are not listed.
- The pricing page is built from the plan documents, and its bullets match the enforced limits.

## Measurements

| Item | Result |
|---|---|
| Probe | Before: 0/5 PASS. After: 5/5 |
| axe | 12 scans. Before: 8 serious results (`definition-list`, `dlitem`; plan and company billing, light and dark). After: 0 violations; the usage card keeps its padding and border in the real page |
| Overflow | 0 at 375 and 320 px |

## Checks

| Check | Result |
|---|---|
| Targeted: `apps.billing portals.employer portals.candidate` | 393 OK before the markup fix (`var/benchmark/targeted-billing-loop.log`); **395 OK** after it (`var/benchmark/targeted-billing-markup.log`) |
| New regression tests | 15 in `apps/billing/tests/test_billing_loop_regressions.py` |
| Full project suite | Ran **3190** tests in 1157 s — **OK** (`var/benchmark/full-suite-billing-loop.log`; 3177 + 13 billing tests present when it started). The two markup tests and the template fix landed during the run and are covered by the 395-test targeted run |
| mypy (`apps`) / Ruff / migrations | mypy clean (357 files); Ruff clean on changed files after sorting one import; `makemigrations --check`: no changes (`Limit.audience` is Python only) |

## Policy decisions needed (documented, not decided)

> **Resolved by the owner on 2026-09-18 — decision D-08** (SRS §0.4, §5.14; Loop-Closure-Ledger §8): Pro starts at once without waiting for payment; Free → Pro starts a new period that day with a full-period invoice; Pro → Free is scheduled for the period end (Pro until then, invoice still owed, date on the plan screen, applied by the renewal); an overdue invoice blocks any new subscription or upgrade, including re-subscribing after an unpaid expiry; no proration or refund. Item 3 (real gateways) stays an external blocker. The list is kept as it was asked.

1. **Self-serve paid plans start before payment.** "Move to Pro" makes the subscription `active` and raises an open invoice, so Pro is usable at once. With B1 fixed, an account that never pays loses it after the period plus 14 days (46 days in the probe), and can then subscribe again. Whether a first period should wait for payment, whether an unpaid expired account may re-subscribe, and whether debt should block it are product rules.
2. **Mid-period plan change is not prorated.** `change_plan` keeps the period, and `issue` is keyed on the period, so moving Free → Pro mid-period returns the period's existing invoice (or none for Free). Moving Pro → Free leaves the Pro invoice open and still owed. Proration and refunds are commercial terms.
3. **Real gateways** (eSewa, Khalti, Fonepay) and a callback URL do not exist (SRS §21). The callback path is exercised only through the test gateway.

## Remaining (not fixed)

- Two nightly runs at the same moment were not concurrency-tested. Invoice idempotency is constraint-backed; the renewal itself is not locked.
- Dunning is one event (`subscription.payment_failed`), as SRS §21 states.
- The plan screen has no way to pay an invoice in-product (bank transfer is recorded by staff in Django admin).
