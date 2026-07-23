"""The predictability meter.

Drawing k of N per candidate makes links differ, but "different" is not
"unpredictable" and the difference is invisible to whoever picks the numbers.
These are the numbers HR is shown, so they need to be right.

  expected shared questions between two papers = k * k/N
  pool exposed after c papers                  = 1 - (1 - k/N)^c
"""

import pytest

from core import predictability as p


def test_whole_pool_means_identical_papers():
    a = p.assess(10, 10)
    assert a["identical"] is True
    assert a["level"] == "none"
    assert "identical" in p.summary(10, 10)


def test_shallow_pool_is_flagged_weak():
    """12 drawing 10 shares 8.3 of 10 — barely randomised at all."""
    a = p.assess(12, 10)
    assert a["overlap"] == pytest.approx(8.3, abs=0.1)
    assert a["level"] == "weak"
    assert "growing the pool" in p.summary(12, 10)


def test_healthy_pool_reads_good():
    """A 4x pool: 2.5 of 10 shared."""
    a = p.assess(40, 10)
    assert a["overlap"] == pytest.approx(2.5, abs=0.1)
    assert a["level"] in ("ok", "good")


def test_the_documented_example_is_accurate():
    """Pool 30, 10 each: ~3.3 shared, ~87% of the pool out after 5 candidates."""
    a = p.assess(30, 10, candidates=5)
    assert a["overlap"] == pytest.approx(3.3, abs=0.1)
    assert a["exposed_pct"] == pytest.approx(87, abs=1)


def test_deeper_pools_leak_more_slowly():
    shallow = p.assess(20, 10, candidates=5)["exposed_pct"]
    deep = p.assess(100, 10, candidates=5)["exposed_pct"]
    assert deep < shallow


def test_suggested_pool_is_four_times_the_paper():
    assert p.assess(12, 10)["suggested_pool"] == 40
    # never suggests shrinking an already-deep pool
    assert p.assess(200, 10)["suggested_pool"] == 200


def test_overlap_never_exceeds_the_paper_size():
    for n in range(1, 60):
        for k in range(1, n + 1):
            a = p.assess(n, k)
            assert 0 <= a["overlap"] <= k
            assert 0 <= a["overlap_pct"] <= 100
            assert 0 <= a["exposed_pct"] <= 100


def test_degenerate_inputs_do_not_explode():
    for n, k in ((0, 0), (0, 5), (5, 0), (1, 1), (-3, 2)):
        a = p.assess(n, k)
        assert isinstance(p.summary(n, k), str)
        assert a["level"] in ("none", "weak", "ok", "good")


def test_summary_is_plain_english_and_ascii():
    s = p.summary(30, 10)
    assert "share" in s and "%" in s
    s.encode("ascii")     # rendered into Streamlit + the dashboard
