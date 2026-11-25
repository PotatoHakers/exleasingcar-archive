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
| Same-day end-time refresh | Every 2 hours for auctions with an explicit source end-time label today | Preserve the original visible end-time label and source server clock. Do not reinterpret an end time as a start time or convert it to UTC until the source timezone is verified. |
| Live-auction refresh | Every 5 minutes for English/open auctions with a known end time within the next 30 minutes or a source FINAL CALL/HOT BID signal | Prefer catalogue/bulk API updates; do not open every vehicle detail page on each cycle. FINAL CALL is a live-priority hint only when the primary auction format is English/open. |
| Detail enrichment | Lower-frequency rotation, preferably during night hours | Refresh slow-changing vehicle metadata and discover photo/report references. Night-time is an optimization, not an assumption that auctions are inactive. |

Auction state and lot state are separate. An auction may remain active while individual lots have finished bidding. A source-confirmed terminal state stops live polling for that lot only. The end of bidding is not proof of a sale.

Auction format, auction phase, and lot countdown are independent signals:

- The auction card's primary format label distinguishes English/open, closed, and fixed-price-only offers. A fixed-price option marker beside an English/closed format is an additional Buy Now option, not a replacement for the primary format.
- FINAL CALL/HOT BID on an auction card may prioritize live refresh only for an English/open auction. The same label on a closed or fixed-price-only card must not activate live bid monitoring.
- A visible label such as "Конец Сегодня 10:28" is an auction end-time label, not proof that bidding has started. Preserve it as source text until the site's server-time timezone is verified.
- Per-lot HOT BID imagery and `counter_to_car_auction_end` timers describe an individual lot; they must not be promoted to auction-level state.
- Fixed-price-only lots are enriched once and then rechecked on the slower detail rotation. The fixed price is appended to history only when amount/currency/source field changes. Do not poll them on the live-bid cadence.

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

## 4. Selected runtime shape: owner's always-on laptop

**Owner decision — 25 November 2026:** recurring infrastructure spend is not acceptable. Use the owner's existing, always-on laptop as the initial collection host. Do not provision a paid VM.

This is a local-first runtime, not a highly available service:

- The collector, SQLite database, Chromium profile, and scheduler state live on the laptop's persistent local disk.
- Keep the live SQLite database and browser profile out of OneDrive, Dropbox, network shares, and other file-synchronization folders.
- The laptop must be powered on, connected to the Internet, and in a usable user session for collection to run. Sleep, shutdown, or loss of connectivity pauses collection. Missed ticks remain overdue and must be resumed in bounded batches; never launch an unbounded catch-up burst.
- Run one collector process at a time. Keep the OS process lock and SQLite transactions.
- The current collector opens a visible Chromium persistent context and uses the local Windows credential-store integration. Run it under the same Windows user profile that owns the browser profile and credentials. Do not run it as a generic background service in a different account or an isolated non-interactive session.
- A future Windows Task Scheduler entry may start bounded runs only after the persisted due-work queue is implemented and manual session-expiry behavior is tested. It must use the same user profile and preserve the interactive browser flow. Scheduling is not enabled by this document.
- Do not register this laptop as a self-hosted GitHub Actions runner. The repository is public, and untrusted pull-request code must not gain access to the authenticated browser, local database, or private R2 credentials.

### GitHub Actions

Keep GitHub Actions limited to source-only Ruff and pytest CI. Standard GitHub-hosted runners are free for public repositories, but each job runs in a fresh virtual environment; they do not preserve the laptop's SQLite database, browser profile, or session. Scheduled Actions can also be delayed or dropped under load. Therefore Actions is not the collector runtime and must not receive Exleasingcar session data or R2 credentials.

### SSH and Dashboard

The Dashboard remains bound to loopback and is intended for use on the laptop itself. It is not a public website and must not be exposed to the Internet.

SSH is for private administration of the laptop, not for publishing the Dashboard. When remote administration is needed, use SSH only over a trusted local network or a private VPN; do not expose an unauthenticated Dashboard or an unrestricted public SSH port. A private SSH tunnel is possible later, but is not required for local Dashboard use.

### Authentication and session expiry

The owner reports that the Exleasingcar session expires after about 15 minutes of inactivity.

- Keep the persistent Chromium profile on the laptop and start the collector under the same interactive Windows account.
- The collector must detect an expired session and stop safely for normal manual login. Do not bypass MFA, CAPTCHA, or any other verification.
- A process that runs continuously may keep the session active, but this has not been proven. Do not assume that scheduled short runs will preserve the session between invocations.
- Before unattended Task Scheduler runs are enabled, rehearse initial login, a 15-minute idle expiry, manual reauthentication, browser restart, and recovery after an interrupted run. If the site requires manual login and nobody is present, collection must pause rather than attempt an authentication workaround.

### SQLite backups and R2

Keep the working SQLite database on the laptop's local disk and use SQLite's backup API to produce consistent snapshots. Do not copy a live database while ignoring its WAL state.

For a zero-spend starting setup, store a second encrypted backup on a separate disk or another existing computer, not beside the only working copy. A backup on the same physical disk is not protection against disk failure.

R2 remains suitable for original photos, inspection reports, derived previews, and potentially a second encrypted backup destination. It is object storage, not a place to run Python/Chromium or host the active SQLite database. Cloudflare currently includes 10 GB-month of Standard storage, 1 million Class A operations, and 10 million Class B operations per month; usage beyond included amounts can be billable. Keep Standard only, retain the project's account-wide usage checks and durable reservation ledger, and enforce the default cap at no more than 50% of the free allowance (or a lower owner-set cap).

Keep the production media bucket private. Put database backups in a separate private backup bucket/destination rather than mixing them with media objects. R2 operations remain disabled until the usage guard is implemented and the owner separately authorizes each specific live operation. Do not send or store R2 credentials in the repository, CI, logs, or chat.

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
6. **Operational rehearsal:** manually run bounded jobs on the laptop; test crash recovery, delayed/missed triggers, the reported 15-minute session expiry, manual reauthentication, and database restoration. Keep the Dashboard loopback-only.
7. **R2 media archive:** only after the separate usage guard and explicit operation-level authorization are ready; no live R2 operations before that.
8. **Release v0.4.0:** only after real-site acceptance, migrations, CI, security, and recovery checks pass.

## 8. Owner decisions and current status

The following runtime choices are now set:

- **Execution host:** the owner's existing, always-on laptop; no paid VM or recurring hosting spend.
- **Administration:** SSH, kept on a trusted local network or private VPN. Dashboard remains local-only and loopback-bound.
- **GitHub Actions:** source-only CI. No live collection runner, private data, browser session, or R2 secret.
- **Authentication:** the current session reportedly expires after about 15 minutes of inactivity. Reauthentication remains a normal interactive login; unattended recovery has not yet been proven.
- **Backups:** use a separate encrypted local disk or another existing computer for the zero-spend baseline. R2 may be considered as an additional private destination after the usage guard is complete and specific live operations are separately authorized.
- **R2:** keep it for object storage (media and, later, a guarded backup destination), not compute or the live SQLite database. No live R2 operation is authorized.

Still open are implementation and acceptance gates, not infrastructure-provider choices: persist and integrate the due-work queue; verify source semantics and real-site coverage; prove session behavior under repeated runs; test backup restoration; and finish the R2 usage guard before any live R2 use.

Until these decisions and source-verification gates are satisfied, collection remains manually started and bounded. No unattended cloud collector or public Dashboard should be enabled.
