# Pricing model: is a new listing below, around, or above market?

Nothing in this repository implements this document. This is the written
answer to the second half of the assignment — "how would you assess whether
a new listing is priced below / around / above market" — and it is meant to
stand on its own, read by someone who has not seen any code. The platform
Tasks 1-6 build is what makes the data this model needs exist in the first
place; this document is what you would build on top of it.

## 1. The core data problem: two datasets, neither sufficient alone

Two sources exist, and each is missing exactly what the other has.

**HM Land Registry Price Paid Data** (published monthly, free, covers
England and Wales) gives the one number that actually matters for training
a price model: what a property *sold* for, as a completed transaction, not
an asking price someone picked. But it gives almost nothing else usable on
its own — an address and postcode, a transaction date, "new build" and
"freehold/leasehold" flags, and a property type coded as detached/
semi/terraced/flats-or-maisonettes/other. No bedroom count, no floor area,
no condition, no description, no photographs. It is ground truth with no
features.

**This platform's own scraped listings** (what Tasks 1-6 collect nightly)
give the opposite: bedrooms, bathrooms, floor area (when stated), a free-text
description, an asking price, a listed date, and — critically, because
`apply_delta` already tracks it — a full price-change history and a
withdrawn/sold-STC/relisted timeline per URL. But an asking price is not a
sale price; it is what an agent and vendor hoped for, and the whole "below/
around/above market" question collapses to a circularity if the model is
trained to predict the quantity it's being asked to assess the deviation
from.

**The actual asset this platform builds, which neither source has alone, is
the join between them over time.** A listing first appears at some asking
price; it may reprice (visible in `listing_history`); it is eventually
withdrawn (visible as a `withdrawn` entry in a `DeltaReport`) — and some
fraction of withdrawals are sales. Matching a withdrawn listing to a Price
Paid completion a few weeks to a few months later, by postcode plus a
fuzzy address match (Price Paid's address strings are free text too — "FLAT
3, 12 HIGH STREET" vs. a listing portal's "Flat 3, 12 High Street, Townname,
POSTCODE" — normalise both to a comparable token form, then match on
postcode + house number/name + a string-similarity threshold on the rest,
accepting that some fraction will not match and must be dropped rather than
forced), produces exactly the row a pricing model needs: **the full feature
set from the listing, paired with the real sale price from Price Paid,
months after the asking price was first seen.** This join is slow to
accumulate — it depends on this platform having been running long enough to
watch listings resolve — which is the actual reason to start collecting
now rather than when the pricing model is wanted.

Where the join fails to find a match (most listings — most property
doesn't turn over in any given window), those rows cannot be used as labelled
training data. They are still usable for inference once the model exists, and
they are the bulk of what the "is this new listing priced right" question
is actually asked about day to day.

**This join has a selection bias worth naming, not discovering later.**
Whether a listing resolves to a matched Price Paid sale at all is not
random with respect to price: a correctly-priced or underpriced listing
sells faster and more often than an overpriced one, which is more likely to
sit, reprice downward, be withdrawn unsold, or relist under a different
agent — none of which produces a Price Paid match in the window this join
looks in. The training set is therefore systematically short of exactly
the overpriced listings the product exists to flag, which biases the
model's learned notion of "expected price" upward relative to true expected
price in whatever segments overpricing is common, and biases it in the
specific direction that makes the model *under*-detect overpricing — the
failure mode that matters most here, not a neutral one. Two mitigations,
not mutually exclusive: (1) explicitly model the probability a listing
matches at all (a second, auxiliary model predicting "does this listing
resolve to a sale within the window," using the same features) and
inverse-weight matched rows by it during training, so an overpriced
listing's rarer matches count for more; or (2) treat every unmatched
withdrawal as **censored**, not absent — known to have *not* sold at its
asking price within the window, which is itself informative about that
price being too high, and worth a survival-analysis-style treatment (e.g.
a Cox model or an accelerated-failure-time model on time-to-sale, with
withdrawal-without-sale as right-censoring) rather than being silently
excluded from everything. Either mitigation is a second pass, not a
blocker to building the join at all; the point of naming it here is that
"the model's residuals look fine on held-out matched data" is not, by
itself, evidence the model is well-calibrated against the full population
of new listings, including the overpriced ones that are disproportionately
absent from that held-out set.

## 2. Target: predict price from Price Paid, not asking price from itself

The model is trained to predict **log(sale price)** from Price Paid-matched
rows, using the listing's own features as of when it was *first listed* —
not its asking price, and not any feature that encodes the asking price
(no "percent below original asking price" feature, no "days since first
listed at price X"). Training on asking price, or on a feature derived from
it, would make the model's "expected price" partly a restatement of
whatever the vendor asked for, and the whole exercise is to tell a new
listing's asking price apart from the market, not confirm it.

Log, not raw price, because UK residential prices span roughly two orders
of magnitude (a one-bed flat in a northern town to a detached house in
London) and errors are naturally multiplicative, not additive — being off
by £20,000 means something completely different on a £150,000 flat than on
a £1.5m house. Modelling log price makes a constant proportional error
across the whole price range, which the eventual 15-20% band around
"around market" (below) naturally wants.

## 3. Features

Drawn from what the listing extractor (`nimbus.extractor`) already
produces, plus geography:

- **Structural**: `bedrooms`, `bathrooms`, `floor_area_sqft` (when
  present — a lot of UK listings omit it; the model must tolerate missing
  values, not drop every row that lacks one), `property_type`, `tenure`.
- **Location**: postcode, resolved to outcode (e.g. `M20`) and to a finer
  unit where Price Paid density supports it (full postcode or postcode
  sector). Location is the single highest-signal feature in UK residential
  pricing by a wide margin — the same three-bed semi varies by a factor of
  3-4x between regions — so it is not optional, and it is also the axis
  along which the comparable-selection step (below) does its real work.
- **Time**: month/quarter of listing, both as a raw feature and — more
  importantly — as the basis for a local house-price-index adjustment: a
  comparable sale from 18 months ago needs inflating/deflating to "now"
  terms before it is comparable at all, using either the UK HPI
  (ONS/Land Registry publish one, regionally) or a locally-fit trend if
  density supports it.
- **Text, cautiously**: description length and a small set of keyword
  flags (e.g. "needs renovation," "chain-free," "no onward chain",
  "immaculate") as weak signals, not a full NLP pipeline — this is a
  gradient-boosted-tree model on structured features, and the payoff of a
  learned text embedding is not obviously worth the added failure surface
  for a model that must also explain, in one sentence, why it said what it
  said.
- **Deliberately excluded**: anything derived from the listing's own asking
  price or price history (see Section 2), and agent identity (an agent
  brand is a weak, legally fraught proxy for things like overpricing
  tendencies that should be measured directly if they matter, not encoded
  as "which agency").

## 4. Comparable selection

Before the full model runs, or alongside it as an interpretable backbone, a
simple comparable-based estimate matters for the same reason an estate
agent's own "comparables" page matters: it's what makes a number legible
to a human. Comparables are selected by:

1. Same `property_type` (a terraced house is not a comparable for a
   detached house of the same bedroom count).
2. Within the same postcode sector, falling back to outcode if too few
   matches (a thin-evidence problem the abstain rule below addresses
   directly).
3. Within ±1 bedroom.
4. Sale date within a rolling window (12-24 months), each adjusted to
   "now" terms via the HPI step in Section 3.
5. Ranked by a weighted distance (no retrieval beats a transparent set of
   hard filters here — there is no larger candidate pool to retrieve from
   than "every Price Paid row in this postcode sector").

The comparable set's median (not mean — sale prices have a long right tail
from the rare mansion skewing a small sample badly) is the sanity-check
number a human reviewer sees next to the model's own estimate; a wide
divergence between the two is itself a signal worth surfacing, not
resolving silently in favour of either.

## 5. Model choice: gradient-boosted trees on log price

**Gradient boosting** (LightGBM or XGBoost; either is fine, LightGBM's
native categorical handling is marginally more convenient for
`property_type`/`tenure`/outcode), not a linear model and not a neural
network, for three concrete reasons specific to this problem:

- Price-feature interactions are strongly non-linear and non-additive — an
  extra bedroom is worth vastly different amounts in different postcodes,
  and a tree ensemble captures that without hand-engineering every
  interaction term a linear model would need.
- The feature set is tabular, modest in width, and the training set is
  going to be small for a while (bounded by how many listings this
  platform has watched turn into a matched sale) — not the regime a deep
  model earns its complexity in.
- Tree ensembles give feature importances and, via SHAP, a defensible
  per-prediction explanation ("this estimate is driven mainly by: fewer
  comparable sales than average in this postcode this quarter; smaller
  floor area than the local median"), which matters for a tool whose
  output is "this listing looks overpriced" being said about someone's
  actual home.

## 6. Conformal prediction for a calibrated interval, not a bare point estimate

A point estimate ("£485,000") invites false confidence; a residential sale
price has genuine, irreducible variance that a single number hides. **Split
conformal prediction** wraps the trained model to produce a interval with a
guaranteed marginal coverage rate, without assuming the residuals are
normally distributed (they are not — price residuals are heavier-tailed and
somewhat heteroscedastic, worse at the high end):

1. Hold out a calibration set, distinct from both training and test, drawn
   from the same time-ordered split discipline as Section 8.
2. Compute the model's residual on each calibration example — ideally a
   *normalised* residual (divide by a locally-estimated typical error size,
   e.g. from a secondary model predicting residual magnitude, or a simple
   bucket-by-price-band scale factor) so the interval naturally widens for
   harder-to-price properties rather than being one fixed width for every
   price band.
3. At inference, take the desired coverage (e.g. 90%) quantile of those
   calibration residuals and place it symmetrically (or asymmetrically, with
   split conformal's natural extension, since under-pricing and
   over-pricing residuals are not symmetric at the top of the market) around
   the new point estimate.

This produces exactly the two-number range the assignment's own example
asks for — "Estimated: £485,000-£510,000" — with a specific, checkable
property: on held-out data, roughly 90% of true sale prices should fall
inside the stated interval. That checkable property is the entire point of
choosing conformal prediction over an ad hoc "± 5%" band: it can be audited
against reality, logged, and re-validated every time the model is retrained,
rather than asserted once and trusted forever.

## 7. The abstain rule: when there isn't enough evidence to say anything

The model must refuse to produce a confident assessment when the evidence
behind it is thin, rather than quietly widening the interval until it's
technically not wrong. Concretely, abstain (report "insufficient comparable
data" rather than a below/around/above verdict) when:

- Fewer than some minimum count (e.g. 5-8) of genuinely comparable sales
  exist in the relevant postcode sector/time window/property-type/bedroom
  bucket — a thin market (a rural postcode, an unusual property type) does
  not get a confident number just because the model is willing to
  extrapolate one.
- The new listing's features fall meaningfully outside the training
  distribution's support — an 8-bedroom country house is not usefully
  assessed against a model trained overwhelmingly on 2-4 bedroom urban/
  suburban stock, even if the model will happily emit a number for it
  anyway. A simple distance-to-nearest-training-neighbour check, or the
  tree ensemble's own leaf-coverage statistics, catches this cheaply.
- The conformal interval computed for this specific prediction (not a
  flat threshold — the per-prediction width from Section 6's
  heteroscedasticity-aware calibration) exceeds some proportion of the
  point estimate itself — if the honestly-calibrated range is "£300,000 to
  £700,000," that is not an assessment worth presenting as one, even though
  it is a true statement about the model's uncertainty.

Abstaining is not a failure mode to minimise toward zero; a system that
never abstains on a genuinely thin market is one that has quietly stopped
being honest about what it knows, which matters more here than it would in
a lower-stakes application, because the output is read as an opinion about
someone's largest asset.

## 8. Leakage avoidance: split by time, not at random

A random train/test split on this data leaks badly, in a way specific to
real estate: nearby properties sell close together in time and share
market conditions (a local price run-up, a rate change, a new transport
link), so a random split puts near-duplicate market conditions on both
sides of the split and the model's measured accuracy becomes partly a
measure of how well it memorised a time period, not how well it predicts
an unseen one.

The split must be **time-ordered**: train on transactions before a cutoff
date, validate on a window immediately after it, test on a later window
still after that — simulating the actual deployment condition (predicting
forward in time from everything known so far) rather than the easier,
misleading condition of interpolating inside a shuffled bag of transactions
from the same months. Any feature derived from "the market at time of
sale" (the HPI adjustment, local comparable counts) must be computed using
only information available as of the listing's own date, not using future
knowledge of how the local market actually moved afterward — the single
most common and most subtle leakage bug in price-prediction setups, and
the one most worth a deliberate unit test.

Retraining cadence follows the same logic: refit periodically (monthly or
quarterly, matched to Price Paid's own publication cadence) on an
expanding or rolling time-ordered window, and re-run the conformal
calibration step every time, since the calibration residuals are only
honest if they come from the model's *current* version evaluated on
recent, representative data.

## 9. Worked example, in the assignment's own format

```
New listing:    £550,000
Estimated:      £485,000-£510,000
Assessment:     Above expected market level
```

How this would actually be produced: the listing (a 3-bed semi in a given
postcode sector, extracted by `nimbus.extractor` from a real crawled page)
is scored by the trained model, giving a point estimate of (say) £497,500.
The conformal interval at 90% coverage, calibrated on recent held-out
sales in comparable postcode sectors/property types, widens that to
£485,000-£510,000. The asking price of £550,000 sits above the *upper*
bound of the interval, not just above the point estimate, which is the
actual decision rule: "above market" is reported when the asking price
exceeds the interval's upper bound (not the point estimate alone), "below
market" when it falls under the lower bound, and "around market" for
anything inside the interval — because the interval, not the point
estimate, is the thing with a calibrated statistical meaning. If instead
fewer than the minimum comparable count existed for this postcode
sector/property-type/bedroom combination, the output would be
`Assessment: insufficient comparable data to assess (n=3 comparable sales
in TOWNCODE_SECTOR, below the minimum of 5)` — the abstain rule from
Section 7, stated honestly rather than papered over with a wide range.

## 10. What this document is not

This is a design for a model that does not exist yet, built on data this
platform has not yet accumulated enough of to train on (the Price-Paid
join needs months of listings resolving before it has a usable sample
size). It is the answer to "how would you build this," not a claim that it
is built, tested, or validated against real outcomes — that validation is
itself the first real piece of work once enough matched training rows
exist, and the honest number to report at that point is the conformal
interval's actual observed coverage rate on held-out data, not an assumed
one.
