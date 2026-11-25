# Collector Runtime and Scheduler Design

**Status:** implementation foundation in place; runtime and source-verification gates remain open for [Issue #45](https://github.com/PotatoHakers/exleasingcar-archive/issues/45).  
**Current application schema:** SQLite v18.  
**Scope:** design and implementation status only. This document does not enable unattended collection, expose the Dashboard, or authorize any R2 operation.

## 1. Product requirements

The collector must support one shared due-work planner with configurable intervals. Current owner-requested defaults are:

| Work type | Default cadence / trigger | Collection behavior |
|---|---|---|
| Initial bootstrap | One-time, explicitly started | Discover every available auction and traverse every catalogue page in bounded, resumable runs. Archive only lots with a qualifying public price. |
| Routine refresh | Every 12 hours | Refresh known auctions/lots, discover newly listed lots, append observations, and preserve history when listings disappear. |
| Pre-start refresh | Once about 2 hours before a verified scheduled start | Refresh eligible lots. Use a scheduled **start** time, never an end time or countdown. |
| Live-auction refresh | Every 5 minutes after trading is positively confirmed | Prefer catalogue/bulk API updates; do not open every vehicle detail page on each cycle. |
| Detail enrichment | Lower-frequency rotation, preferably during night hours | Refresh slow-changing vehicle metadata and discover photo/report references. Night-time is an optimization, not an assumption that auctions are inactive. |

Auction state and lot state are separate. An auction may remain active while individual lots have finished bidding. A source-confirmed terminal state stops live polling for that lot only. The end of bidding is not proof of a sale.

Price categories must remain separate: displayed auction price, verified fixed Buy Now price, raw API `highest_offer`, seller `minimum_price`, and seller `indicative_price`. Account-specific `user_offer` and `actual_car_price` must never enter the archive.

## 2. Current implementation (verified against master)

The earlier version of this document described the pre-PR #92 collector. That description is obsolete. The repository now includes the following foundation:

### Implemented

- **Bounded planner primitives:** `scheduler.py` defines work types and deterministic priority ordering for live refresh, bootstrap, pre-start, routine refresh, and detail enrichment. It supports explicit item/time/page/live-lot/detail-page budgets and a reservation for background work to reduce starvation.
- **Hard per-run limits:** `RunBudget` uses a monotonic deadline and atomically reserves catalogue pages, live-lot updates, and detail-page visits. Failed attempts still consume their reserved budget.
- **Resumable catalogue scans:** SQLite schema v18 stores scan generations, completed page checkpoints, page payloads, API response payloads, and cursors. An unfinished scan resumes from its saved cursor. A page is checkpointed only after successful parsing and ingestion.
- **Durable API queues:** bulk responses are replayed only after archive writes commit. API cursor advancement is durable. Unknown sale modes are split into per-lot deferred updates and replayed after detail enrichment establishes the sale type.
- **Per-process exclusion:** the manual collector uses a non-blocking OS-level lock before database initialization or browser startup. The default lock is shared across CLI runs in the checkout.
- **Read-only validation report:** `real_site_validation_report.py` checks schema version, SQLite integrity, foreign keys, expected tables, catalogue progress, pending deferred API work, price coverage, and media-discovery aggregates.
- **Source-only CI:** GitHub Actions runs Ruff and pytest. It does not authenticate to Exleasingcar or access the private archive/R2.

### Still not implemented

- The pure planner is **not yet integrated** with `ExleasingcarCollector.run()` as a persisted due-work scheduler. The CLI still processes the selected auction list in its current order; it does not yet calculate and persist every auction's next due time for all cadences.
- There is no host timer, systemd service, cloud worker, or unattended collection.
- The collector still launches a visible persistent Chromium profile and uses the current local credential-store integration. This is not yet a supported cloud authentication design.
- There is no verified auction-level scheduled start field or authoritative auction-level live/inactive signal.
- The current API fields `auction_end_at`, `remaining_seconds`, `live_bidding`, `auction_phase`, and `bid_status` are lot-level observations. They must not be repurposed as an auction start-time signal.
- The Dashboard remains loopback-only and has no remote authentication/TLS layer. Do not expose it publicly.

## 3. Real-site validation snapshot — 24 November 2026

A controlled manual run used auction `211304` and the separate database `real_site_validation_v18.sqlite3`. The subsequent read-only report returned:

- SQLite schema v18; `integrity_check=ok`; zero foreign-key violations.
- 93 auction cards discovered on the site list; one auction processed.
- 148 unique catalogue lots and 148 recognized detail links.
- Six detail pages attempted and checked; 142 detail pages had not yet been checked.
- 66 photo source references across the six checked lots; no PDF report references in those six details.
- Eight API `highest_offer` observations across six lots; six seller-reference observations; six displayed-price observations.
- One completed bootstrap scan with one catalogue page checkpoint.
- 142 per-lot API updates still pending because their sale modes had not yet been established.

The structural report passed. This is **not** full real-site acceptance: only six of 148 detail pages were checked, and only one of the 93 discovered auction cards was collected. The report does not prove the meaning of `highest_offer`, seller reference fields, scheduled start, or auction-level trading state. It also does not establish that all 66 photo references are correctly associated without a sample audit.

No media binaries were downloaded and no R2 operation was performed.

### API response capture follow-up

The same real-site collector log showed two HTTP 200 API responses whose bodies were unavailable after navigation (`Network.getResponseBody: No resource with given identifier found`). PR #98 contains a bounded retry at `requestfinished`, a longer wait for detail-page API requests, and a stop-before-next-navigation guard. It must pass CI and owner review/merge before another real-site run is used to assess whether this issue is resolved.

### Next controlled data work

Continue using the same temporary database so its catalogue checkpoints and deferred queue are retained. Process additional detail pages in bounded batches, verify that the pending deferred API count falls as sale modes become known, and compare representative catalogue/API/detail observations. Do not use the tiny three-car auction `209633` as the primary sample; it is not representative of the wider archive.

The next full-archive bootstrap must cover all available auctions, not just `211304`. The current collector still needs a round-robin/persisted due-work integration before repeated unattended runs can safely manage this across the whole site.

## 4. Recommended runtime shape

**Recommendation:** one dedicated private cloud VM with encrypted persistent block storage, running a single collector service. The provider is deliberately not selected.

- Run independently of the owner's personal PC.
- Keep SQLite, the persistent Chromium profile, and scheduler state on a durable private local filesystem attached to the VM. Do not put the live SQLite database on a network share or file-sync folder.
- Use one collector process at a time; retain the OS process lock and SQLite transactions.
- Trigger the worker with a host scheduler such as a systemd timer only after persisted due-work scheduling and operational rehearsal are complete.
- Keep GitHub Actions limited to public-source Ruff/pytest CI. Do not grant public pull-request workflows access to authenticated sessions, the database, or cloud secrets. Do not use a self-hosted runner for untrusted public PRs.
- Keep the Dashboard bound to loopback. Use a private VPN or SSH tunnel for initial administration. Remote Dashboard hosting requires authentication, HTTPS at the serving edge, and authorization tests before exposure.

A GitHub Actions schedule may only trigger a private service; it must not become the collector's runtime or persistence layer.

### Authentication and secrets

The current local credential-store/browser setup is not a cloud authentication design.

- Establish the first login interactively through private remote access on the selected host.
- Verify that ordinary Exleasingcar login and the persistent Chromium session remain stable there, including the site's normal additional verification flow if requested.
- Keep the browser profile on the encrypted private volume, accessible only to the collector service account.
- Keep credentials and service secrets in the host's protected secret store, never in Git, SQLite rows, command-line arguments, CI artifacts, or logs.
- Never bypass CAPTCHA, MFA, or another verification step.

If the site session cannot be maintained safely and reliably, stop; do not attempt to defeat the site's authentication controls.

### SQLite backups and R2

- Keep one SQLite database on the VM's durable local volume and use SQLite's backup API for consistent backups. Do not copy a live database while ignoring WAL state.
- Store encrypted backups in a separate private backup destination. Test restoration before schema migrations.
- R2 remains the selected object store for original photos, reports, and derived previews. The production bucket must remain private.
- Database backups are not implicitly stored in the media bucket.
- This design does not authorize R2 reads, writes, usage queries, bucket checks, uploads, downloads, or preview serving. Those require separate explicit owner authorization for the specific operation and the project's usage guard.

## 5. One persisted due-work planner

The existing planner is a pure bounded selection component. Runtime integration still needs a persisted source of due work.

One invocation should ask a single planner to choose among:
1. positively confirmed live-auction refreshes;
2. unfinished one-time bootstrap work;
3. pre-start refreshes for verified start times;
4. overdue 12-hour routine refreshes and catalogue discovery;
5. lower-frequency detail enrichment and safe retries.

The five-minute live work must not permanently starve bootstrap, routine refresh, or detail enrichment. Keep a configurable background reservation or weighted fairness rule. Within each class, select the oldest overdue item first with stable identifier tie-breaks.

Persist at least:
- work type, auction/lot target, due time, next due time, last attempted time, last successful time, and last result;
- scan generation, page cursor, completed-page checkpoints, and full-scan completion;
- run ID, start/end time, selected work, consumed budgets, deferred items, and stop reason;
- scheduled start value, source, observation time, timezone, and verification status after a trustworthy source is confirmed.

Missed timer ticks must result in overdue work being selected once, not unbounded catch-up bursts. A partial catalogue scan must never imply that an absent lot was sold or unavailable. Missing lots may be evaluated only after a complete scan, and disappearance alone is not proof of sale.

## 6. Source-verification gates

Before enabling auction-level scheduling, a controlled real-site comparison must establish:

1. Where the scheduled **trading start** is exposed (catalogue HTML, auction detail page, or API), its timezone, and whether it changes.
2. Which source field positively confirms that the auction has started and later become inactive. Do not use `FINAL CALL`, an end time, or a stale lot countdown as a substitute.
3. Whether catalogue/bulk API values are reliable during active trading. Compare displayed prices and per-lot states against the corresponding detail pages.
4. The public meaning, if any, of `highest_offer`. Keep it named as the raw API field until verified.
5. The exact meaning and visibility of `minimum_price` and `indicative_price`. Seller references alone do not qualify a lot as having a public starting price.
6. How the catalogue represents a lot whose bidding ended while other lots in the same auction remain active.
7. How a completed/closed lot with and without a verified public Buy Now price is represented.

For every observation, retain the raw field name, source (catalogue HTML, detail HTML, or API), normalized value, and observation timestamp. Missing or stale values are unknown/unavailable, not zero. Never store `user_offer` or `actual_car_price` as archive prices.

## 7. Remaining implementation and acceptance phases

1. **Finish API capture reliability:** review and merge PR #98 after successful CI; then repeat a bounded real-site run against a representative auction and verify API response capture.
2. **Complete real-site qualification:** process the pending detail queue in bounded runs; verify price categories, completed-lot behavior, excluded hidden-bid lots, Buy Now eligibility, and media associations.
3. **Complete bootstrap coverage:** traverse all 93+ discovered auctions and all catalogue pages using a persisted fair work queue. Do not mark bootstrap complete globally until every discovered auction scan is complete.
4. **Integrate the planner:** persist due-work state and connect live, bootstrap, pre-start, routine, and detail work to the real collector adapters. Add tests for fairness, missed triggers, stale/missing signals, crash resume, and one-writer operation.
5. **Choose and provision the runtime:** select provider/region, monthly budget, private access, durable disk, secret handling, and separate encrypted backup destination.
6. **Operational rehearsal:** manually run bounded jobs on the private host; test crash recovery, delayed/missed triggers, session expiry, and database restoration. Keep the Dashboard private.
7. **R2 media archive:** only after the separate usage guard and explicit operation-level authorization are ready; no live R2 operations before that.
8. **Release v0.4.0:** only after real-site acceptance, migrations, CI, security, and recovery checks pass.

## 8. Owner decisions still needed

- **Cloud provider and region:** preferred provider/region, or permission to compare EU providers; maximum monthly budget for compute and persistent disk.
- **Private administration:** VPN or SSH tunnel for login and maintenance; whether remote Dashboard access is required at all.
- **Database backups:** separate destination, retention period, and maximum monthly budget. The backup destination must not be the R2 media bucket by default.
- **Authentication:** whether ordinary login requires MFA/additional confirmation and how often the session currently expires. Do not send passwords, cookies, or browser-profile files.
- **R2 operations:** when the media workstream is ready, authorize the specific read/write/usage-check operation separately; no credentials need to be sent in chat.

Until these decisions and source-verification gates are satisfied, collection remains manually started and bounded. No unattended cloud collector or public Dashboard should be enabled.
