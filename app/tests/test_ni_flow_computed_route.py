"""Code owns the computed route (blind10, 2026-10-05): 'moon phase' was classed computed_only by the
model and failed asking for a YYYY-MM-DD date. The computed source builds countdowns only, so an ask
takes that route only when its own words carry a countdown cue or a date."""
from smartbrain_3000 import ni_flow


def test_only_a_countdown_or_dated_ask_takes_the_computed_route() -> None:
    takes = ni_flow._countdown_ask
    for ask in ("countdown to 2026-12-25", "days until Christmas", "how many days until my birthday",
                "how long until the election", "days left until 2027-01-01", "2026-11-03"):
        assert takes(ask), ask
    for ask in ("moon phase", "sunset time", "what day is it", "moon phase tonight",
                "next full moon", "when is the winter solstice"):
        assert not takes(ask), ask
