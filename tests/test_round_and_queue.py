"""Regression checks for show-round selection and waiting-room queue entry."""

import pytest

from bots.thaiticketmajor import TTMState, match_rounds

PERF_MULTI = "https://event.thaiticketmajor.com/performance/multi.html"
PERF_PRESALE = "https://event.thaiticketmajor.com/performance/presale.html"
PERF_QUEUE = "https://event.thaiticketmajor.com/performance/queue-link.html"
BOOKING = "https://booking.thaiticketmajor.com/booking/3m/zones.php?query=700"
ENTRY = "https://wait.thaiticketmajor.com/entry?c=ttm&e=test"
QUEUE_VIEW = "https://wait.thaiticketmajor.com/view?c=ttm&e=test"


# ----------------------------------------------------------------------
# Pure matching
# ----------------------------------------------------------------------

ROUNDS = [
    {"value": "1001", "text": "ส. 16 ส.ค. 2569 18:00", "time": "18:00"},
    {"value": "1002", "text": "ส. 16 ส.ค. 2569 20:00", "time": "20:00"},
    {"value": "1201", "text": "อา. 17 ส.ค. 2569 18:00 น.", "time": "18:00 น."},
]


@pytest.mark.parametrize(
    "query, expected",
    [
        ("1002", "1002"),
        ("20:00", "1002"),
        ("20.00", "1002"),
        ("20:00 น.", "1002"),
        ("17 ส.ค. 18:00", "1201"),
        ("อา. 17 ส.ค. 2569 18:00", "1201"),
        ("ส. 16 ส.ค.   2569\n20:00", "1002"),
    ],
)
def test_match_rounds_finds_expected(query, expected):
    matches = match_rounds(query, ROUNDS)
    assert matches and matches[0]["value"] == expected


def test_match_rounds_does_not_match_inside_other_times():
    # "8:00" must not silently pick the 18:00 round.
    assert match_rounds("8:00", ROUNDS) == []
    assert match_rounds("0:00", ROUNDS) == []


# ----------------------------------------------------------------------
# Performance page round links
# ----------------------------------------------------------------------

def test_round_in_second_event_block_is_selectable(site, make_bot):
    site.page.goto(PERF_MULTI)
    bot = make_bot(show_round="20:00")

    assert bot.handle_performance_page(site.page) is True
    assert "rdId=2001" in site.page.url
    assert bot.selected_round["value"] == "2001"


def test_round_with_different_query_in_same_block_is_selectable(site, make_bot):
    site.page.goto(PERF_MULTI)
    bot = make_bot(show_round="17 ส.ค. 18:00")

    assert bot.handle_performance_page(site.page) is True
    assert "query=702" in site.page.url and "rdId=1201" in site.page.url


def test_auto_round_keeps_first_enabled_round(site, make_bot):
    site.page.goto(PERF_MULTI)
    bot = make_bot()

    assert bot.handle_performance_page(site.page) is True
    assert "rdId=1001" in site.page.url


def test_ticket_type_restricts_blocks(site, make_bot):
    site.page.goto(PERF_MULTI)
    bot = make_bot(ticket_type="Seated")

    assert bot.handle_performance_page(site.page) is True
    assert "rdId=2001" in site.page.url


def test_requested_round_not_open_yet_waits_instead_of_crashing(site, make_bot):
    site.page.goto(PERF_MULTI)
    bot = make_bot(show_round="08:00")

    handled = bot.handle_performance_page(site.page)

    assert handled is True
    # still on the performance page, it reloaded rather than clicking another round
    assert "/performance/" in site.page.url
    assert site.count("zones.php") == 0
    assert site.count("/performance/multi.html") == 2


def test_unknown_round_raises_with_available_list(site, make_bot):
    site.page.goto(PERF_MULTI)
    bot = make_bot(show_round="23:45")

    with pytest.raises(Exception) as err:
        bot.handle_performance_page(site.page)

    assert "23:45" in str(err.value)
    assert site.count("zones.php") == 0


def test_slow_queue_navigation_is_not_aborted_by_fallback_goto(site, make_bot):
    site.delays["/booking/3m/zones.php"] = 10
    site.page.goto(PERF_QUEUE)
    bot = make_bot()

    assert bot.handle_performance_page(site.page) is True
    assert "zones.php?query=695" in site.page.url
    # One click, one navigation: the old code re-requested the URL via goto
    assert site.count("zones.php") == 1


def test_round_link_opening_new_tab_is_followed(site, make_bot):
    site.page.goto(PERF_QUEUE + "#newtab")
    bot = make_bot()

    assert bot.handle_performance_page(site.page) is True
    new_page = bot.take_pending_page()
    assert new_page is not None
    assert "zones.php?query=695" in new_page.url
    # no duplicate request through fallback goto in the original tab
    assert site.count("zones.php") == 1
    assert "/performance/" in site.page.url


# ----------------------------------------------------------------------
# Booking page select#rdId
# ----------------------------------------------------------------------

def test_dropdown_selects_requested_round_and_confirms(site, make_bot):
    site.page.goto(BOOKING)
    bot = make_bot(show_round="20:00")

    assert bot.select_show_round(site.page) is True
    assert "rdId=1002" in site.page.url
    assert bot.detect_state(site.page) == TTMState.ZONE


def test_dropdown_auto_skips_placeholder_and_disabled(site, make_bot):
    site.page.goto(BOOKING)
    bot = make_bot()

    assert bot.select_show_round(site.page) is True
    assert "rdId=1001" in site.page.url


def test_dropdown_keeps_round_chosen_on_performance_page(site, make_bot):
    # Performance page click already landed on rdId=1002.
    site.page.goto(BOOKING + "&rdId=1002")
    bot = make_bot()
    bot.selected_round = {"value": "1002", "text": "20:00"}

    assert bot.select_show_round(site.page) is True
    assert "rdId=1002" in site.page.url
    # no extra reload of the booking page
    assert site.count("zones.php") == 1


def test_dropdown_auto_keeps_round_already_in_url(site, make_bot):
    site.page.goto(BOOKING + "&rdId=1002")
    bot = make_bot()

    assert bot.select_show_round(site.page) is True
    assert "rdId=1002" in site.page.url
    assert site.count("zones.php") == 1


def test_dropdown_disabled_round_is_reported(site, make_bot):
    site.page.goto(BOOKING)
    bot = make_bot(show_round="17 ส.ค. 18:00")

    with pytest.raises(Exception) as err:
        bot.select_show_round(site.page)

    assert "ปิด" in str(err.value) or "disabled" in str(err.value).lower()


def test_dropdown_hidden_native_select_still_selects(site, make_bot):
    site.page.goto(BOOKING + "&hidden=1")
    bot = make_bot(show_round="20:00")

    assert bot.select_show_round(site.page) is True
    assert "rdId=1002" in site.page.url


# ----------------------------------------------------------------------
# Queue / waiting room
# ----------------------------------------------------------------------

def test_presale_page_text_is_not_treated_as_queue(site, make_bot):
    site.page.goto(PERF_PRESALE)
    bot = make_bot()

    assert bot._queue_detected(site.page) is False
    assert bot.detect_state(site.page) == TTMState.WAIT_SALE


def test_queue_page_text_is_in_queue_not_entry(site, make_bot):
    site.page.goto(QUEUE_VIEW)
    bot = make_bot()

    assert bot.detect_state(site.page) == TTMState.IN_QUEUE
    assert bot._queue_detected(site.page) is True


def test_join_waiting_room_click_is_confirmed(site, make_bot):
    site.page.goto(ENTRY)
    bot = make_bot()

    assert bot.detect_state(site.page) == TTMState.QUEUE_ENTRY
    assert bot.handle_waiting_room_entry(site.page) is True
    assert site.page.evaluate("window.joinClicks") == 1


def test_join_button_disabled_is_waited_for_not_synthetic_clicked(site, make_bot):
    site.page.goto(ENTRY + "&readyAfter=1500")
    bot = make_bot()

    assert bot.handle_waiting_room_entry(site.page) is True
    assert site.page.evaluate("window.joinClicks") == 1


# ----------------------------------------------------------------------
# Countdown output goes to the in-place progress line, not the log
# ----------------------------------------------------------------------

def test_format_remaining():
    from bots.thaiticketmajor import ThaiTicketMajorBot

    assert ThaiTicketMajorBot.format_remaining(125) == "02:05"
    assert ThaiTicketMajorBot.format_remaining(3725) == "1:02:05"
    assert ThaiTicketMajorBot.format_remaining(-3) == "00:00"


def test_countdown_uses_progress_not_log(make_bot):
    progress = []
    bot = make_bot(progress_callback=progress.append)

    bot._sleep_with_countdown(2.2, lambda left: f"left {int(left)}")

    assert len(progress) >= 2
    assert bot.logs == []


def test_progress_falls_back_to_log_without_callback(make_bot):
    bot = make_bot()
    bot.progress("hello")
    assert bot.logs == ["hello"]


# ----------------------------------------------------------------------
# Zone map (image map with 40 zones)
# ----------------------------------------------------------------------

ZONE_MAP = "https://booking.thaiticketmajor.com/booking/3m/zonemap.php?query=700&rdId=1001"


def test_read_zones_reads_all_areas(site, make_bot):
    site.page.goto(ZONE_MAP)
    bot = make_bot()

    names = [z["name"] for z in bot.read_zones(site.page)]

    assert len(names) == 40
    assert names[:2] == ["A1", "A2"] and "HH" in names and "SB" in names


def test_zone_is_opened_with_a_real_click(site, make_bot):
    site.page.goto(ZONE_MAP)
    bot = make_bot(selection_mode="ZONE", preferred_zone="D8")

    assert bot.handle_zone(site.page) is True
    clicks = site.page.evaluate("window.zoneClicks")
    assert clicks == [{"zone": "D8", "trusted": True}]
    assert bot.detect_state(site.page) == "SEAT"


def test_sold_out_zone_with_popup_fails_over_to_next_priority(site, make_bot):
    site.page.goto(ZONE_MAP + "&soldout=A1&popup=1")
    bot = make_bot(selection_mode="ZONE", preferred_zone="A1,A2")

    assert bot.handle_zone(site.page) is True
    assert bot.selected_zone == "A2"
    zones = [c["zone"] for c in site.page.evaluate("window.zoneClicks")]
    assert zones == ["A1", "A2"]


def test_sold_out_zone_without_popup_fails_over(site, make_bot):
    site.page.goto(ZONE_MAP + "&soldout=A1")
    bot = make_bot(selection_mode="ZONE", preferred_zone="A1,A2")
    bot.zone_seat_wait_ms = 800

    assert bot.handle_zone(site.page) is True
    assert bot.selected_zone == "A2"


def test_all_zones_closed_returns_false_for_next_scan_cycle(site, make_bot):
    site.page.goto(ZONE_MAP + "&soldout=A1,A2")
    bot = make_bot(selection_mode="ZONE", preferred_zone="A1,A2")
    bot.zone_seat_wait_ms = 500

    assert bot.handle_zone(site.page) is False
    assert bot._scan_cycle == 2


def test_round_button_enabled_by_js_is_seen_without_reload(site, make_bot):
    site.page.goto(PERF_PRESALE)
    bot = make_bot()
    site.page.evaluate(
        """
        setTimeout(() => {
            const row = document.querySelector('#section-event-round .row');
            row.classList.add('row-enable');
            row.querySelector('.col-btn').innerHTML =
              '<a class="btn" href="https://booking.thaiticketmajor.com/booking/3m/zones.php?query=695">18:00</a>';
        }, 400);
        """
    )

    opened = bot._sleep_with_countdown(
        5,
        lambda left: "wait",
        until=lambda: bot._enabled_round_count(site.page) > 0,
    )

    assert opened is True
    assert site.count("/performance/presale.html") == 1
