# Routine refresh worker (Windows)

The routine worker is a bounded, one-shot process. Windows Task Scheduler can
invoke it periodically; it does not install a service or keep Chromium running
between invocations.

## Preconditions

1. Complete global auction discovery and bootstrap first.
2. Keep the existing archive SQLite file and Chromium profile in place.
3. Run under the same Windows account that owns the saved Credential Manager
   entry and the persistent Chromium profile.
4. Keep the task configured to run only while that user is logged on. The
   collector uses a visible browser and may require ordinary interactive
   re-authentication when the website session expires.

## Inspect due work without opening a browser

From the repository root in PowerShell:

```powershell
python -m exleasingcar_archive.routine_runner --database data/exleasingcar_archive.sqlite3 --profile-dir data/browser_profile --raw-dir data/raw/collector --plan-only
```

The worker creates a separate sidecar file named
`exleasingcar_archive.routine-schedule.sqlite3` next to the archive DB. This
file stores only routine cadence and retry metadata; it is not the archive DB.

## Run one bounded batch

```powershell
python -m exleasingcar_archive.routine_runner --database data/exleasingcar_archive.sqlite3 --profile-dir data/browser_profile --raw-dir data/raw/collector --max-auctions 10 --max-run-seconds 900 --max-catalog-pages-per-run 40 --max-live-lots-per-run 500 --max-detail-pages-per-run 60
```

A successful routine catalogue scan is scheduled again 12 hours after
completion. Failures use exponential retry delays from 5 minutes up to 6 hours.
If a scan is paused by a resource budget, it remains due and resumes from its
persisted catalogue cursor on the next invocation. The same shared run budget
applies across the whole batch.

## Task Scheduler

Create a task that runs at user logon and repeats every 30 minutes indefinitely.
Use the repository root as **Start in** and the command above as the action.
Set **Run only when user is logged on**; do not enable elevated privileges or
store a separate password in the task. The worker exits immediately when no
auction is due, so the 30-minute trigger is only a wake-up/check interval.

Do not configure concurrent instances. The existing collector process lock
prevents two collector workers from writing the archive simultaneously.

## Current scope

This worker performs routine catalogue/API refreshes for auctions whose
bootstrap queue entry is completed and whose source URL is known. It does not
implement verified pre-start timing, five-minute live-auction cadence,
automatic session recovery beyond the site's ordinary login flow, media
downloads, R2 operations, or a full 24-hour acceptance test. Do not treat
auction end time as auction start time.
