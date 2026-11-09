"""
Tests for the 2026-11-08 fix: a Thursday market crash, covered Friday, was re-run
on Sunday because the recap carried a fresh timestamp and a new headline.

1. filter_repeat_events drops re-reports of events earlier episodes covered.
2. Writers and scorers get publish times, a computed market calendar and DATE_RULE.
3. filter_stale_articles drops far-future dates.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import mc_collect  # noqa: E402
import mc_facts  # noqa: E402
import mc_generate  # noqa: E402
import mc_utils  # noqa: E402

BIZ = "Business & Markets"
ET = ZoneInfo("America/New_York")

PRIOR = [
    {"title": "A brutal sell-off sends the S&P 500 down 4.4%", "source": "AP News",
     "description": "", "_episode_date": "2026-11-06"},
]


def _llm(content):
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: _llm.calls.append(kw) or response)))
    return lambda: client


def _scored():
    return {
        BIZ: [
            {"title": "Nasdaq ends -5.5%", "source": "TradingView", "score": 65,
             "published": "Sun, 08 Nov 2026 09:05:49 GMT"},
            {"title": "Berkshire doubles buybacks", "source": "CNBC", "score": 55},
            {"title": "Gold at $4,070", "source": "Wire", "score": 30},
            {"title": "Backfill story", "source": "Wire", "score": 20},
        ],
        "NY Sports": [{"title": "Knicks win", "source": "ESPN", "score": 90}],
    }


def _run(content, prior=None, total=2):
    _llm.calls = []
    with patch.object(mc_collect, "OPENAI_API_KEY", "k"), \
            patch.object(mc_collect, "_LLMClient", _llm(content)), \
            patch.object(mc_collect, "_load_recent_episode_articles",
                         return_value=PRIOR if prior is None else prior):
        return mc_collect.filter_repeat_events(_scored(), total=total)


def _titles(result, cat=BIZ):
    return [a["title"] for a in result[cat]]


class TestFilterRepeatEvents:

    def test_repeat_dropped_and_next_story_moves_up(self, caplog):
        with caplog.at_level("INFO", logger="mikecast"):
            result = _run(json.dumps([{"id": 0, "prior": "P0"}]))
        assert _titles(result)[0] == "Berkshire doubles buybacks"
        assert "Nasdaq ends -5.5%" not in _titles(result)
        assert "already covered 2026-11-06" in caplog.text

    def test_no_repeats_returns_input(self):
        assert _titles(_run("[]")) == [a["title"] for a in _scored()[BIZ]]

    def test_ny_sports_never_sent_or_touched(self):
        result = _run(json.dumps([{"id": 0, "prior": "P0"}]))
        assert _titles(result, "NY Sports") == ["Knicks win"]
        assert "Knicks win" not in _llm.calls[0]["messages"][0]["content"]

    def test_prompt_has_both_lists_and_the_new_development_exception(self):
        _run("[]")
        prompt = _llm.calls[0]["messages"][0]["content"]
        assert "P0 (2026-11-06): A brutal sell-off" in prompt
        assert "posted Sun Nov 8, 4:05 AM ET" in prompt
        assert "next market session" in prompt

    def test_flag_must_name_a_real_prior_story(self):
        for bad in ([{"id": 0, "prior": "P99"}], [{"id": 0}], [{"id": 0, "prior": "x"}], [{"id": 77, "prior": "P0"}], [0]):
            assert len(_titles(_run(json.dumps(bad)))) == 4, bad

    def test_flagging_most_candidates_keeps_everything(self, caplog):
        flagged = [{"id": i, "prior": "P0"} for i in range(3)]
        with caplog.at_level("WARNING", logger="mikecast"):
            assert len(_titles(_run(json.dumps(flagged)))) == 4
        assert "implausible" in caplog.text

    def test_fails_open_on_bad_output(self):
        assert len(_titles(_run("not json"))) == 4

    def test_no_prior_episodes_skips_the_call(self):
        assert len(_titles(_run("[]", prior=[]))) == 4
        assert _llm.calls == []

    def test_disable_flag_skips_the_call(self):
        with patch.dict(os.environ, {"MIKECAST_REPEAT_FILTER": "0"}):
            assert len(_titles(_run(json.dumps([{"id": 0, "prior": "P0"}])))) == 4
        assert _llm.calls == []

    def _vote(self, replies):
        """Run the filter with one scripted reply per vote."""
        pending = iter(replies)
        lock = threading.Lock()

        def create(**_kw):
            with lock:
                content = next(pending)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(mc_collect, "OPENAI_API_KEY", "k"), \
                patch.object(mc_collect, "_LLMClient", lambda: client), \
                patch.object(mc_collect, "_load_recent_episode_articles", return_value=PRIOR):
            return _titles(mc_collect.filter_repeat_events(_scored(), total=2))

    REPEAT = json.dumps([{"id": 0, "prior": "P0"}])

    def test_two_of_three_votes_drop(self):
        assert "Nasdaq ends -5.5%" not in self._vote([self.REPEAT, self.REPEAT, "[]"])

    def test_one_of_three_votes_keeps(self, caplog):
        with caplog.at_level("INFO", logger="mikecast"):
            assert "Nasdaq ends -5.5%" in self._vote([self.REPEAT, "[]", "[]"])
        assert "only 1 of 3 votes" in caplog.text

    def test_one_failed_vote_still_decides(self):
        assert "Nasdaq ends -5.5%" not in self._vote([self.REPEAT, self.REPEAT, "not json"])

    def test_too_few_votes_keeps_everything(self):
        assert len(self._vote([self.REPEAT, "not json", "not json"])) == 4

    def test_pipelines_call_the_filter(self):
        for name in ("mikecast_briefing.py", os.path.join("crew", "research_crew.py")):
            with open(os.path.join(_ROOT, name), encoding="utf-8") as fh:
                assert "filter_repeat_events(scored" in fh.read(), name


class TestLoadRecentEpisodes:

    def _load(self, tmp_path):
        with patch.object(mc_collect, "TODAY", "2026-11-08"), \
                patch.object(mc_collect, "S3_BUCKET", ""), \
                patch.object(mc_collect, "DATA_DIR", tmp_path):
            return mc_collect._load_recent_episode_articles()

    def _write(self, tmp_path, day, articles):
        (tmp_path / f"{day}.json").write_text(json.dumps({"articles": articles}), encoding="utf-8")

    def test_previous_days_only_without_sports(self, tmp_path):
        self._write(tmp_path, "2026-11-08", {BIZ: [{"title": "today"}]})
        self._write(tmp_path, "2026-11-07", {BIZ: [{"title": "sat"}], "NY Sports": [{"title": "game"}]})
        self._write(tmp_path, "2026-11-05", {BIZ: [{"title": "thu"}]})
        self._write(tmp_path, "2026-11-04", {BIZ: [{"title": "too old"}]})
        loaded = self._load(tmp_path)
        assert [(a["title"], a["_episode_date"]) for a in loaded] == [
            ("sat", "2026-11-07"), ("thu", "2026-11-05"),
        ]

    def test_corrupt_day_is_skipped(self, tmp_path):
        (tmp_path / "2026-11-07.json").write_text("{not json", encoding="utf-8")
        self._write(tmp_path, "2026-11-06", {BIZ: [{"title": "fri"}]})
        assert [a["title"] for a in self._load(tmp_path)] == ["fri"]

    def test_s3_mode_reads_episode_keys(self):
        with patch.object(mc_collect, "TODAY", "2026-11-08"), \
                patch.object(mc_collect, "S3_BUCKET", "bucket"), \
                patch.object(mc_collect, "s3_load_json", return_value=None) as load:
            assert mc_collect._load_recent_episode_articles() == []
        assert [c.args for c in load.call_args_list] == [
            ("bucket", "data/2026-11-07.json"),
            ("bucket", "data/2026-11-06.json"),
            ("bucket", "data/2026-11-05.json"),
        ]


class TestMarketCalendar:

    def test_sunday_after_the_crash_points_to_friday(self):
        line = mc_facts.market_calendar_line(datetime(2026, 11, 8, 6, 30, tzinfo=ET))
        assert "Sunday, is NOT a trading day" in line
        assert "Friday, November 6, 2026" in line

    def test_before_and_after_the_close_on_a_weekday(self):
        monday_am = datetime(2026, 11, 9, 6, 30, tzinfo=ET)
        monday_pm = datetime(2026, 11, 9, 16, 5, tzinfo=ET)
        assert mc_facts.last_completed_us_session(monday_am) == date(2026, 11, 6)
        assert mc_facts.last_completed_us_session(monday_pm) == date(2026, 11, 9)

    def test_published_nyse_holidays(self):
        assert mc_facts.nyse_holidays(2026) == {
            date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
            date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
            date(2026, 11, 26), date(2026, 12, 25),
        }

    def test_new_year_on_saturday_keeps_friday_open(self):
        assert date(2027, 1, 1) in mc_facts.nyse_holidays(2027)  # Friday, closed
        assert date(2021, 12, 31) not in mc_facts.nyse_holidays(2021)
        assert mc_facts.is_us_trading_day(date(2021, 12, 31))

    def test_session_skips_a_holiday(self):
        friday_after_thanksgiving = datetime(2026, 11, 27, 6, 30, tzinfo=ET)
        assert mc_facts.last_completed_us_session(friday_after_thanksgiving) == date(2026, 11, 25)


class TestDatesReachPrompts:

    ARTICLE = {"title": "Nasdaq ends -5.5%", "source": "TradingView",
               "published": "Sun, 08 Nov 2026 09:05:49 GMT", "url": "https://x.test/a"}

    def test_format_published_is_eastern_with_weekday(self):
        assert mc_utils.format_published_et(self.ARTICLE["published"]) == "Sun Nov 8, 4:05 AM ET"

    def test_unparseable_date_formats_empty(self):
        assert mc_utils.format_published_et("whenever") == ""
        assert mc_utils.format_published_et(None) == ""

    def test_writer_context_has_published_line(self):
        assert "Published: Sun Nov 8, 4:05 AM ET" in mc_generate._build_articles_context({BIZ: [self.ARTICLE]})

    def test_writer_context_omits_line_without_a_date(self):
        assert "Published:" not in mc_generate._build_articles_context({BIZ: [{"title": "t"}]})

    def test_scorer_batch_has_published_line(self):
        batch = [dict(self.ARTICLE, _score_id=1)]
        assert "Published: Sun Nov 8, 4:05 AM ET" in mc_collect._build_scoring_prompt(batch)

    def test_prompt_block_has_calendar_and_date_rule(self):
        block = mc_facts.prompt_block()
        assert "MARKET CALENDAR:" in block and "DATE RULE:" in block


class TestFutureDates:

    def _run(self, delta):
        pub = format_datetime(datetime.now(timezone.utc) + delta)
        return mc_collect.filter_stale_articles({BIZ: [{"title": "t", "published": pub, "source": "S"}]})[BIZ]

    def test_far_future_dropped(self, caplog):
        with caplog.at_level("WARNING", logger="mikecast"):
            assert self._run(timedelta(days=5)) == []
        assert "1 future-dated" in caplog.text

    def test_mislabeled_timezone_skew_kept(self):
        assert len(self._run(timedelta(hours=5))) == 1
