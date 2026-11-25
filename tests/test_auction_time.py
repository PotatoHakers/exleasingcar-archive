from exleasingcar_archive.auction_time import format_auction_time_for_user


def test_site_datetime_is_shifted_one_hour_for_user():
    assert (
        format_auction_time_for_user("2026-11-25 11:59")
        == "2026-11-25 12:59"
    )


def test_today_clock_label_is_shifted_without_losing_its_context():
    assert (
        format_auction_time_for_user("Конец Сегодня 12:58")
        == "Конец Сегодня 13:58"
    )


def test_clock_shift_that_crosses_midnight_advances_relative_day():
    assert (
        format_auction_time_for_user("Конец Сегодня 23:30")
        == "Конец Завтра 00:30"
    )
    assert (
        format_auction_time_for_user("End Tomorrow 23:30")
        == "End the day after tomorrow 00:30"
    )


def test_unrecognized_text_and_missing_values_are_safe():
    assert format_auction_time_for_user("FINAL CALL") == "FINAL CALL"
    assert format_auction_time_for_user(None) is None
