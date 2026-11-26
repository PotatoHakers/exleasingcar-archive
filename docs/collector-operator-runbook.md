# Collector validation and operator runbook

This document describes the safe, manually triggered path from local
validation to a future private runtime. It does not enable a schedule.

## Current operating guarantees

- The collector remains manually started.
- Each CLI invocation takes an OS-level, non-blocking process lock. A second
  invocation using the same lock path exits without opening the browser.
- The default lock file is `data/collector.lock`, shared by all CLI runs in
  this checkout even when a temporary validation database is selected. Use
  `--lock-file` only when a deliberately separate runtime is required. The
  operating system releases the lock when the owning process exits; the file
  itself is not a stale-process marker and should not be deleted while a
  collector may be running.
- The database, Chromium profile, and raw files remain local. Do not put them
  in Git, GitHub Actions artifacts, or a public/shared folder.
- HTML snapshots stay disabled by default.
- No R2 read, write, bucket check, media upload, or media download is performed
  by the validation procedure below.

## First global catalogue bootstrap (schema v19)

The first global run is still a manually started, local Windows process. It does
not install a scheduled task and does not use GitHub Actions as a collector
runtime. Keep the SQLite database, Chromium profile, and credentials on the
local PC.

Use a dedicated long-lived database path for the archive. Do not point the
first global pass at an old temporary validation database such as
`fresh_api_test.sqlite3`; retain existing databases separately until their
contents have been reviewed.

From PowerShell in the repository root:

```powershell
$env:PYTHONPATH = "src"
python -m pip install -r requirements-collector.txt
python -m playwright install chromium
python check_exleasingcar_keyring.py
```

The keyring check asks you to manually enter the expected login/password and
prints only match results. It does not change or reveal the saved credentials.
The collector still uses the ordinary visible Chromium login; complete any
CAPTCHA or additional verification manually.

### 1. Discover all auction-list pages

Start with discovery-only. It stores every parsed auction card and its source
URL in SQLite, checkpoints each list page, and fills the persistent bootstrap
queue. It does not start per-auction catalogue scans:

```powershell
python -m exleasingcar_archive.collector `
  --database data/exleasingcar_archive.sqlite3 `
  --bootstrap `
  --discover-only `
  --max-auctions 0 `
  --max-run-seconds 900 `
  --max-catalog-pages-per-run 40 `
  --max-live-lots-per-run 0 `
  --max-detail-pages-per-run 0
```

If the list has more pages than the per-run budget permits, rerun the same
command. Discovery resumes from the last committed page. A page is checkpointed
only after its auction cards have been parsed, persisted, and queued. Empty
pages, navigation failures, and pagination cycles do not silently mark the
discovery complete.

### 2. Inspect the persistent queue

```powershell
python -m exleasingcar_archive.collector `
  --database data/exleasingcar_archive.sqlite3 `
  --plan-only
```

This prints discovery and queue totals, opens no Chromium window, and performs
no collection.

### 3. Run the initial catalogue/API bootstrap

```powershell
python -m exleasingcar_archive.collector `
  --database data/exleasingcar_archive.sqlite3 `
  --bootstrap `
  --max-auctions 0 `
  --max-detail-pages 10 `
  --max-run-seconds 900 `
  --max-catalog-pages-per-run 40 `
  --max-live-lots-per-run 500 `
  --max-detail-pages-per-run 60
```

`--max-auctions 0` means all currently due queue entries, not unlimited
network activity: the page, API-lot, detail-page, and wall-clock budgets still
apply. The queue chooses the least-recently attempted auction first, so a large
auction cannot permanently hold the front of the queue. Re-run the same command
to continue the remaining work. A clean budget stop leaves the current auction
in progress with its catalogue cursor saved; a failed auction receives a
bounded retry delay and a sanitized error category.

Use `--auction-id <ID>` to run a single known queue entry. Use
`--rediscover-auctions` together with `--bootstrap` to force a fresh
auction-list discovery pass. It adds newly discovered auctions to the queue;
already completed bootstrap entries are not reset. Do not use this as a
substitute for the future routine-refresh scheduler.

### Current scope and limitations

- This PR adds persistent global auction-list discovery and the one-time
  catalogue/API bootstrap queue. It does **not** enable the 12-hour routine
  cadence, the two-hour same-day cadence, the five-minute live cadence, or an
  unattended Windows Task Scheduler job.
- The existing catalogue scan still performs the bounded detail rotation.
  Bootstrap completion means the auction catalogue scan completed; it does
  not mean every vehicle detail page, photo binary, or report content has been
  archived.
- The current Dashboard is local-only. Keep it bound to loopback; do not
  publish it to the internet.
- R2 and media downloading remain outside this first global bootstrap.

After the first batch, run `--plan-only` and
`python real_site_validation_report.py data/exleasingcar_archive.sqlite3`.
Review the queue totals before continuing with larger batches.

## Controlled real-site validation

The owner has approved a bounded validation on the current PC using an
explicitly named temporary database. Never use the normal working archive or
the collector's default `data/fresh_api_test.sqlite3` path for this check.

From the repository root in PowerShell, activate the project's existing
virtual environment, then run one representative auction with conservative
limits. Replace `<AUCTION_ID>` with the auction ID selected by the owner:

```powershell
$env:PYTHONPATH = "src"
python -m exleasingcar_archive.collector `
  --database data/real_site_validation_v19.sqlite3 `
  --auction-id <AUCTION_ID> `
  --max-auctions 1 `
  --max-detail-pages 6 `
  --max-run-seconds 300 `
  --max-catalog-pages-per-run 10 `
  --max-live-lots-per-run 100 `
  --max-detail-pages-per-run 6
```

Use the site's ordinary interactive login flow through the existing local
browser profile. Do not share the profile, password, keyring contents, cookies,
or raw authenticated page snapshots. If the site asks for an additional
verification step, complete it normally; do not bypass it.

The representative sample should include, where available:

1. An auction that is positively shown as actively trading.
2. A lot with a public fixed Buy Now price.
3. A closed hidden-bid lot without Buy Now, which must remain excluded.
4. A lot whose bidding has ended while another lot in the same auction remains
   live, if the selected auction contains that case.

If the chosen auction does not contain one of these cases, record that the case
was not present; do not broaden the run automatically. Keep
`--save-html-snapshots` off. Do not run the media downloader or any R2 command.

After the run, generate a privacy-preserving structural report:

```powershell
python real_site_validation_report.py data/real_site_validation_v18.sqlite3
```

The report opens SQLite read-only and prints aggregate counts only. Review that
it reports schema v19, a successful SQLite integrity check, zero foreign-key
violations, and the expected scan/deferred-queue tables.

For semantic validation, compare the source page and the corresponding
Dashboard values locally. Record the source field name, displayed value,
source (catalogue HTML, detail HTML, or API), and observation time for:

- displayed auction price;
- fixed Buy Now price;
- `minimum_price` and `indicative_price` (keep these as seller references
  unless their public meaning is confirmed);
- raw `highest_offer` (do not relabel as a public bid until verified);
- scheduled auction start and the page/API field that supplies it;
- auction-level active/inactive signal;
- per-lot bidding state and any terminal state.

Do not record `user_offer` or `actual_car_price`. A missing price or timer is
unknown, not zero. The end of bidding is not proof of sale.

Share only the report's aggregate terminal output and a short redacted summary
of the source-field comparisons. Do not upload the temporary SQLite database,
browser profile, raw HTML, cookies, credentials, or unfiltered API responses.

## Decisions required before a private scheduled runtime

The collector must run independently of the owner's PC. Before provisioning a
host, decide:

1. **Host and budget:** preferred cloud provider/region and the maximum monthly
   budget for a small always-on VM with encrypted persistent storage.
2. **Private access:** VPN or SSH tunnel for initial interactive login and
   private Dashboard access. The current Dashboard must remain loopback-only;
   do not expose it publicly.
3. **Backup destination:** where encrypted SQLite backups should live,
   separately from the private R2 media bucket.
4. **Authentication feasibility:** whether Exleasingcar's ordinary login and
   session remain stable on the chosen host/browser. This must be verified
   interactively before automation.

R2 is already the selected binary media store, but that decision does not
authorize any live R2 operation as part of this work.

## Not enabled

No systemd timer, cloud VM, scheduled collection, GitHub Actions collector,
Dashboard remote access, or R2 operation is enabled by this runbook. GitHub
Actions remains source-only Ruff/pytest CI.
