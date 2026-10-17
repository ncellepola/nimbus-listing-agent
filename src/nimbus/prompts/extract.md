# Nimbus listing extraction (prompt v1)

You are extracting one structured residential-sale listing from a single
estate-agent page. You must call the `emit_listing` tool exactly once with
your answer; do not reply with plain text.

Site: `__DOMAIN__`
Page URL: `__URL__`

This site's onboarding recipe (written by a previous agent run on this same
site, and not guaranteed correct) recorded these field hints. Use them as a
starting point, not as ground truth — the recipe's own notes may warn you
about quirks specific to this site:

__FIELD_HINTS__

The page content below has already been converted from HTML to markdown
(headings kept as `#`/`##`/...). It may contain a price-filter dropdown, a
search form, or other page chrome listing many unrelated pound amounts —
these are NOT this listing's price. Read the heading or status line next to
the true price carefully; never pick the largest, smallest, or first pound
amount you see anywhere on the page.

For `price_raw`, copy the exact status-and-price text as it appears near
the top of the listing, verbatim (e.g. "For Sale £650,000", "Sold Subject
to Contract £415,000", "To Let £1,000 PCM", "POA"). Do not normalise it or
compute a number yourself — a deterministic parser downstream of you turns
this text into a price.

If this page is a rental/letting, a sold/let listing, a commercial
listing, a search/index page, or otherwise not an open residential sale
listing, still call `emit_listing` with your best-effort fields and an
honest `price_raw` (e.g. "£1,000 PCM" or "Sold STC") — a downstream
deterministic check uses that text to reject it. Do not try to make that
judgement call yourself.

---

__PAGE_TEXT__
