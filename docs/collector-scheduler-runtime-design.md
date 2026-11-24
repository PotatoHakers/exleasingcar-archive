# Collector Runtime and Scheduler Design

**Status:** proposal for [Issue #45](https://github.com/PotatoHakers/exleasingcar-archive/issues/45)  
**Scope:** architecture and implementation plan only. This document does not enable unattended collection, change the database schema, expose the Dashboard, or perform any R2 operation.

## 1. Product requirements

The planner must support these work types. Intervals are configuration, with the owner's current values as defaults.

| Work type | Default cadence / trigger | Collection behavior |
|---|---|---|
| Initial bootstrap | One-time, explicitly started | Discover every available auction and walk all catalogue pages in bounded batches. Qualify only lots with an accepted public value. |
| Routine refresh | Every 12 hours | Refresh known auctions and their known lots while they remain listed. Also discover newly listed auctions/lots through the catalogue index. |
| Pre-start refresh | Once about 2 hours before a verified scheduled start | Refresh the eligible lots in that auction. Use a scheduled **start** time, never an end time/countdown. |
| Live-auction refresh | Every 5 minutes while active trading is positively confirmed | Prefer catalogue/bulk API data. Update many lots without opening every detail page. |
| Detail enrichment | Opportunistic night-time rotation, plus a lower-frequency fallback | Open a bounded number of eligible lot detail pages for slow-changing metadata and media source references. Night-time is a preference, not an assumption that no auctions are active. |

The five-minute cadence is intentional. The site timer is usually visible only during roughly the final 15 seconds, so a separate one-minute polling loop is not useful. Save a timer only when the source actually supplies one; a missing timer must not block collection or be guessed.

Auction state and lot state are separate. An auction may remain active while individual lots have finished. A source-confirmed terminal state stops live polling only for that lot; it must not stop updates for other lots in the same auction. The end of bidding is not proof of a sale.

## 2. Verified current implementation and gaps

The current code is a bounded local collector, not yet a scheduled worker:

- `ExleasingcarCollector.run()` opens a persistent Chromium profile with `headless=False`. Authentication reads credentials from Windows Credential Manager through `keyring`.
- One run processes selected auctions; `_collect_auction()` walks catalogue pagination from the beginning. It has a detail-page limit, but no overall run-time budget or persisted catalogue-page cursor.
- Detail-page refresh has a weekly retry/rotation mechanism. That is useful as an enrichment mechanism, but it is not the requested 12-hour catalogue refresh.
- The `auctions` table stores auction identity, labels, first/last-seen timestamps, but no verified scheduled trading start.
- `auction_events` stores per-lot `auction_end_at`, `remaining_seconds`, `live_bidding`, source, and observation time. These are lot-level observations; they cannot safely substitute for an auction start time.
- `ParsedAuctionListing` currently has no scheduled-start field. Auction-level active trading is not yet backed by a separately verified authoritative source signal.
- The collector currently processes a catalogue scan as one pagination loop. It does not persist enough scan progress to safely resume a partially completed catalogue sweep.
- The existing GitHub Actions workflow is source-only Ruff/pytest CI. A GitHub-hosted runner is ephemeral and has no durable browser profile or local SQLite database between jobs.
- The Dashboard defaults to loopback, but the current HTTP server has no authentication or TLS. It must not be exposed directly on a public interface.

These gaps mean that the cadence policy can be designed now, but unattended execution must wait for the runtime, authentication, and source-verification gates below.

## 3. Recommended runtime shape

**Recommendation:** one dedicated private cloud VM with an encrypted persistent block volume, running a single collector service. The provider is deliberately not selected in this proposal.

- Run the collector on that VM, independently of the owner's personal PC.
- Keep the SQLite database, browser profile, and scheduler progress on the VM's durable private volume. Use a local filesystem on that volume, not a network share or a file-sync folder for the live SQLite database.
- Use one collector process at a time. A host-level process lock prevents overlapping invocations; SQLite transactions protect each state update. The worker is the single writer.
- Trigger the worker with a host scheduler (for example, a systemd timer) at the configured five-minute live-work cadence. Every invocation asks the same planner what is due; a delayed or missed trigger is recovered by checking persisted due times on the next invocation.
- Run GitHub Actions only for source CI. Do not give public pull-request workflows access to the authenticated browser, database, secrets, or the private runtime. Do not put this collector on a self-hosted runner that executes untrusted pull-request code.
- Keep the Dashboard bound to loopback. Remote access, if needed, should initially use a private VPN or SSH tunnel. Do not expose the current unauthenticated HTTP server; remote hosting requires authentication, HTTPS at the serving edge, and authorization tests first.

A GitHub Actions schedule could at most be a trigger for an independently hosted private service. It should not be the collector's runtime or persistence layer.

### Authentication and secrets

The current Windows Credential Manager integration is a local-machine dependency and is not a cloud authentication design. The cloud host needs a supported, manually established Exleasingcar session and a controlled re-authentication procedure.

- Establish the first login interactively through private remote access; verify whether the site session remains valid in the intended headless Chromium environment before enabling unattended runs.
- Persist the browser profile only on the encrypted private volume, with access restricted to the collector service account.
- Keep passwords and any service secrets in the host's secret store / protected service credentials, never in Git, a database row, command-line arguments, CI artifacts, or logs.
- Do not copy the current Windows browser profile or credentials to GitHub.

If the site's login or verification flow cannot be kept safely and reliably in the proposed headless environment, stop at the controlled validation stage and revisit the runtime design rather than bypassing verification.

### SQLite, backups, and R2

- Keep one SQLite database on the VM's durable local volume. Use SQLite's supported backup API for consistent backups; do not copy a live database file while ignoring its WAL state.
- Take encrypted backups to a separate private backup destination and test restoration before schema migrations. The backup destination/provider is still a decision; do not assume the media bucket is automatically the database backup target.
- Cloudflare R2 is the selected object store for binary media. Keep the bucket private and credentials in the runtime secret store. The media pipeline remains separate from scheduler activation.
- This proposal does not authorize live R2 reads, writes, bucket checks, uploads, downloads, or usage queries.

## 4. One bounded planner

Use one planner that returns a bounded batch of work items. Do not create independent full-scan loops for each cadence.

Each run has validated, configurable budgets:

- maximum wall-clock duration;
- maximum catalogue pages;
- maximum lots handled by a live batch;
- maximum detail pages;
- allowed work modes and their intervals;
- freshness/staleness limits for auction and lot observations;
- night enrichment window and its maximum share of the run.

The planner reads persisted state, calculates what is due, and chooses work. It does not infer state from a schedule tick alone. Missed intervals make work overdue once; they do not cause an unbounded burst of back-to-back catch-up runs.

### Priority and fairness

1. Positively confirmed live-auction refreshes, due every five minutes.
2. The one-time bootstrap until every discovered auction catalogue has been fully traversed.
3. The pre-start refresh for auctions with a verified scheduled start in the configured two-hour window.
4. The 12-hour refresh of known auctions/lots and catalogue discovery.
5. Night-time detail enrichment and lower-priority retries.

Priority 1 must not consume the entire run indefinitely. Reserve a configurable part of each run for overdue bootstrap/routine work and detail enrichment. Within a class, process the oldest overdue item first; break ties deterministically by auction ID and page/lot ID. The planner reports deferred work and its reason so the next invocation can resume fairly.

### Progress and idempotency

Persist at least:

- work type, target auction/lot, due time, next-due time, last successful time, last attempted time, and last result;
- the current catalogue scan generation, page cursor/URL, completed pages, and whether the full scan completed;
- run ID, start/end time, selected priorities, consumed page/time budget, result, and stop reason;
- the scheduled-start value together with its source, observation timestamp, and verification status once a reliable source is found.

Checkpoint after each successfully parsed catalogue page. A run stopped by its time/page budget resumes from its saved cursor. Mark an auction scan complete only after all discovered catalogue pages have completed. Never mark a lot unavailable or sold because it was absent from a partial scan. Missing lots are only classified after a complete scan, and disappearance alone is not a sale signal.

Use an idempotency key for a pre-start refresh (auction ID plus the verified scheduled-start observation/version) so a delayed trigger does not repeat the same pre-start action. A new verified start time creates a new due event.

## 5. Source-verification gates

Do not implement auction-level scheduling based on assumptions. A controlled, bounded real-site check must establish:

1. Where the site's **scheduled trading start** is actually displayed or returned (catalogue HTML, auction detail, or API), its timezone, and whether it changes.
2. Which source field/page positively confirms that auction trading has started and later become inactive. Do not infer this from `FINAL CALL`, an end time, or a stale per-lot countdown.
3. Whether the catalogue/bulk API reliably exposes current public prices and per-lot status during live trading. Compare a sample against the corresponding detail page and record source plus observation time.
4. The public meaning of `highest_offer`. Keep it labelled as a raw API field until the controlled comparison confirms it represents a public bid. Do not use account-specific `user_offer` or `actual_car_price` as archive prices or qualification evidence.
5. The exact meaning and visibility of `minimum_price` and `indicative_price`. Do not treat seller reference values as public starting prices without evidence.
6. How completed lots are represented in the catalogue while other lots in the auction remain live.

For each source observation, preserve the raw field name, normalized value, source, and observation timestamp. A missing or stale live price is unavailable/stale, not zero. If catalogue data is not reliable for a lot, leave it out of the five-minute price path and let a lower-frequency detail refresh revisit it.

The current qualification rule remains separate: accept a positive public displayed auction price, a verified positive Buy Now price, or a positive public bid whose source semantics have been confirmed. A zero placeholder or seller reference alone is not sufficient.

## 6. Proposed implementation phases

1. **Owner decision:** agree on the private VM/provider and budget, persistent-volume/backups, authentication/session approach, and remote Dashboard access policy.
2. **Source validation:** use an explicitly named temporary SQLite database and bounded real-site runs to verify the source fields listed above. Keep R2 operations disabled.
3. **Planner foundation:** implement configuration validation, due-work calculation, deterministic priority/fairness, run budgets, progress checkpoints, and process locking. Test with synthetic data only.
4. **Collector adapters:** split lightweight catalogue/API refresh from detail enrichment; add safe resume for catalogue pagination; persist source-confirmed auction/lot state independently.
5. **Operational rehearsal:** manually trigger bounded runs on the private host, simulate missed triggers/crashes/session expiry, verify backup restoration, and inspect that no private data enters logs.
6. **Enable scheduling:** only after owner acceptance of source validation, host security, persistence, backup, and operational tests.

## 7. Decisions still needed

- Which cloud provider/region and monthly budget should host the dedicated VM?
- Which private access method should be used for initial login and Dashboard access (VPN or SSH tunnel)?
- Does the site support a stable authenticated headless Chromium session on that host?
- Which verified source field supplies scheduled trading start and auction-level live/inactive state?
- Where should encrypted SQLite backups live, separately from the R2 media bucket?

Until those decisions are made, keep collection manually triggered and bounded. No scheduled cloud collector should be enabled by this design document alone.
