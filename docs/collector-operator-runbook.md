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
  --database data/real_site_validation_v18.sqlite3 `
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
it reports schema v18, a successful SQLite integrity check, zero foreign-key
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
