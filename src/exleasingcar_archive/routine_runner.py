"""Run due routine catalogue refreshes from the persisted bootstrap inventory.

This command is a bounded one-shot worker intended to be invoked periodically
by Windows Task Scheduler. It does not install a background service.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from .collector import (
    AUCTION_LIST_URL,
    DEFAULT_DATABASE,
    DEFAULT_PROFILE_DIR,
    DEFAULT_RAW_DIR,
    ExleasingcarCollector,
)
from .database import ArchiveDatabase, BootstrapQueueItem
from .models import AuctionCardMarker
from .process_lock import CollectorAlreadyRunning, CollectorProcessLock
from .routine_schedule import RoutineSchedule
from .scheduler import RunBudget, RunLimits


def _completed_bootstrap_items(database: ArchiveDatabase) -> tuple[BootstrapQueueItem, ...]:
    """Load completed bootstrap inventory without changing its queue state."""
    with database._connect() as connection:
        rows = connection.execute(
            """
            SELECT auction.id AS database_auction_id,
                   auction.exleasingcar_auction_id, auction.source_auction_name,
                   auction.source_auction_country_code,
                   auction.source_auction_country_name,
                   auction.auction_type_label, auction.auction_status_label,
                   auction.source_url, queue.status, queue.attempts,
                   queue.last_attempted_at
            FROM auction_bootstrap_queue AS queue
            JOIN auctions AS auction ON auction.id = queue.auction_id
            WHERE queue.status = 'completed' AND auction.source_url IS NOT NULL
            ORDER BY auction.exleasingcar_auction_id
            """
        ).fetchall()
        items = []
        for row in rows:
            markers = connection.execute(
                """
                SELECT icon_filename, marker_group, label
                FROM auction_markers
                WHERE auction_id = ? AND is_present = 1
                ORDER BY id
                """,
                (int(row["database_auction_id"]),),
            ).fetchall()
            items.append(
                BootstrapQueueItem(
                    auction_id=str(row["exleasingcar_auction_id"]),
                    source_auction_name=row["source_auction_name"],
                    source_auction_country_code=row["source_auction_country_code"],
                    source_auction_country_name=row["source_auction_country_name"],
                    auction_type_label=row["auction_type_label"],
                    auction_status_label=row["auction_status_label"],
                    source_url=str(row["source_url"]),
                    markers=tuple(
                        AuctionCardMarker(
                            icon_filename=str(marker["icon_filename"]),
                            marker_group=str(marker["marker_group"]),
                            label=(
                                str(marker["label"])
                                if marker["label"] is not None
                                else None
                            ),
                        )
                        for marker in markers
                    ),
                    status=str(row["status"]),
                    attempts=int(row["attempts"]),
                    last_attempted_at=(
                        str(row["last_attempted_at"])
                        if row["last_attempted_at"] is not None
                        else None
                    ),
                )
            )
    return tuple(items)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one bounded batch of due 12-hour routine auction refreshes."
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--schedule-database", type=Path)
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--auction-list-url", default=AUCTION_LIST_URL)
    parser.add_argument("--max-auctions", type=int, default=10)
    parser.add_argument("--max-detail-pages", type=int, default=30)
    parser.add_argument("--page-delay", type=float, default=1.5)
    parser.add_argument("--max-run-seconds", type=float, default=900)
    parser.add_argument("--max-catalog-pages-per-run", type=int, default=40)
    parser.add_argument("--max-live-lots-per-run", type=int, default=500)
    parser.add_argument("--max-detail-pages-per-run", type=int, default=60)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--save-html-snapshots", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    if args.max_auctions < 1:
        raise SystemExit("--max-auctions must be a positive integer")
    if args.max_detail_pages < -1:
        raise SystemExit("--max-detail-pages must be -1, 0 or a positive integer")
    if args.page_delay < 0:
        raise SystemExit("--page-delay cannot be negative")
    try:
        limits = RunLimits(
            max_duration_seconds=args.max_run_seconds,
            max_catalog_pages=args.max_catalog_pages_per_run,
            max_live_lots=args.max_live_lots_per_run,
            max_detail_pages=args.max_detail_pages_per_run,
        )
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"Invalid per-run collection limit: {exc}") from exc

    if not args.database.is_file():
        raise SystemExit(f"Archive database not found: {args.database}")
    schedule_path = args.schedule_database or args.database.with_name(
        args.database.stem + ".routine-schedule.sqlite3"
    )
    lock_path = args.database.parent / "collector.lock"

    try:
        with CollectorProcessLock(lock_path):
            database = ArchiveDatabase(args.database)
            database.initialize()
            schedule = RoutineSchedule(schedule_path)
            schedule.initialize()
            now = datetime.now(timezone.utc)
            candidates = _completed_bootstrap_items(database)
            for item in candidates:
                schedule.register(item.auction_id, due_at=now)
            due = schedule.due(now=now, limit=args.max_auctions)

            print(f"Completed bootstrap auctions: {len(candidates)}")
            print(f"Routine refreshes due: {len(due)}")
            print(f"Schedule database: {schedule_path}")
            if args.plan_only:
                for item in due:
                    print(f"  due: {item.auction_id} (failures={item.consecutive_failures})")
                print("Plan only: browser not opened; no collection performed.")
                return 0
            if not due:
                print("No routine refresh is due.")
                return 0

            collector = ExleasingcarCollector(
                database,
                args.raw_dir,
                page_delay_seconds=args.page_delay,
                save_html_snapshots=args.save_html_snapshots,
            )
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise SystemExit(
                    "Playwright is not installed. Run: "
                    "python -m pip install -r requirements-collector.txt"
                ) from exc

            processed = 0
            run_budget = RunBudget(limits)
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(args.profile_dir),
                    headless=False,
                    viewport={"width": 1440, "height": 1000},
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    collector.recorder.attach(context)
                    page.goto(
                        args.auction_list_url,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                    if not collector.recorder.wait_for_pending(page):
                        raise RuntimeError("Auction-list page has pending source requests.")
                    if not collector._is_logged_in(page):
                        collector._authenticate(
                            page,
                            reason="no authenticated account header was found at startup",
                        )
                        page.goto(
                            args.auction_list_url,
                            wait_until="domcontentloaded",
                            timeout=60_000,
                        )
                        if not collector.recorder.wait_for_pending(page):
                            raise RuntimeError(
                                "Auction-list page has pending requests after login."
                            )
                    else:
                        collector._click_continue_if_visible(page)

                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    snapshot_dir = args.raw_dir / stamp
                    snapshot_dir.mkdir(parents=True, exist_ok=True)
                    collector.api_cursor = len(collector.recorder.responses)

                    for scheduled in due:
                        schedule.mark_started(
                            scheduled.auction_id, at=datetime.now(timezone.utc)
                        )
                        item = next(
                            candidate
                            for candidate in candidates
                            if candidate.auction_id == scheduled.auction_id
                        )
                        listing = collector._bootstrap_listing(item)
                        try:
                            collector._collect_auction(
                                listing,
                                page,
                                snapshot_dir,
                                max_detail_pages=args.max_detail_pages,
                                run_budget=run_budget,
                            )
                        except Exception as exc:
                            delay = schedule.mark_failed(
                                scheduled.auction_id,
                                at=datetime.now(timezone.utc),
                                error=type(exc).__name__,
                            )
                            print(
                                f"  {scheduled.auction_id}: failed; retry in "
                                f"{int(delay.total_seconds())} seconds ({type(exc).__name__})"
                            )
                            continue

                        with database._connect() as connection:
                            latest_scan = connection.execute(
                                """
                                SELECT runs.status
                                FROM catalog_scan_runs AS runs
                                JOIN auctions ON auctions.id = runs.auction_id
                                WHERE auctions.exleasingcar_auction_id = ?
                                  AND runs.scan_type = 'routine'
                                ORDER BY runs.id DESC LIMIT 1
                                """,
                                (scheduled.auction_id,),
                            ).fetchone()
                        if latest_scan is not None and latest_scan["status"] == "completed":
                            schedule.mark_succeeded(
                                scheduled.auction_id, at=datetime.now(timezone.utc)
                            )
                            print(f"  {scheduled.auction_id}: routine scan completed")
                        else:
                            print(
                                f"  {scheduled.auction_id}: scan remains resumable; "
                                "it stays due for the next invocation"
                            )
                        processed += 1
                        budget_state = run_budget.snapshot()
                        if budget_state.expired or (
                            budget_state.catalog_pages_used >= limits.max_catalog_pages
                            or budget_state.live_lots_used >= limits.max_live_lots
                            or budget_state.detail_pages_used >= limits.max_detail_pages
                        ):
                            print("  Batch stopped: shared per-run resource budget exhausted.")
                            break
                finally:
                    context.close()

            print(f"Routine refresh batch finished: {processed}/{len(due)} attempted")
            return 0
    except CollectorAlreadyRunning as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
