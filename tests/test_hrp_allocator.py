"""Tests for jarvis.risk.hrp_allocator.

What this module actually computes is **inverse-variance weighting**. It has
never implemented Hierarchical Risk Parity — the original 48-line version had
the same two methods, `get_correlation_distance` has never been called by
anything, and there is no clustering, quasi-diagonalisation or recursive
bisection anywhere. The docstrings said otherwise, which is how a backtest
report came to label the output "HRP".

These tests pin the real behaviour, including two properties that are wrong for
a risk allocator and are fixed here:

* a zero-variance (flat) series took the *entire* allocation, because
  `variances[variances <= 0] = 1e-6` turns "no measurable risk" into
  "one million times the weight";
* correlation is ignored entirely, so two assets that move together still each
  receive a full inverse-variance weight.
"""

import numpy as np
import pandas as pd
import pytest

from jarvis.risk.hrp_allocator import HierarchicalRiskParityAllocator as HRP


# ±1 and ±2: sample variance of `b` is exactly 4x that of `a`, so the
# inverse-variance split is exactly 0.8 / 0.2.
def _ab():
    a = np.array([1.0, -1.0, 1.0, -1.0])
    b = np.array([2.0, -2.0, 2.0, -2.0])
    return pd.DataFrame({"A": a, "B": b})


# ---------------------------------------------------------------------------
# get_correlation_distance
# ---------------------------------------------------------------------------

class TestCorrelationDistance:
    def test_perfectly_positive_correlation_is_zero_distance(self):
        cov = np.array([[1.0, 1.0], [1.0, 1.0]])
        d = HRP.get_correlation_distance(cov)
        assert d[0, 1] == pytest.approx(0.0)

    def test_zero_correlation_is_sqrt_of_a_half(self):
        cov = np.array([[1.0, 0.0], [0.0, 1.0]])
        d = HRP.get_correlation_distance(cov)
        assert d[0, 1] == pytest.approx(np.sqrt(0.5))

    def test_perfectly_negative_correlation_is_maximum_distance(self):
        cov = np.array([[1.0, -1.0], [-1.0, 1.0]])
        d = HRP.get_correlation_distance(cov)
        assert d[0, 1] == pytest.approx(1.0)

    def test_the_diagonal_is_always_zero(self):
        cov = np.array([[4.0, 1.5, -0.5],
                        [1.5, 2.0, 0.3],
                        [-0.5, 0.3, 9.0]])
        assert np.all(np.diag(HRP.get_correlation_distance(cov)) == 0.0)

    def test_the_result_is_symmetric(self):
        cov = np.array([[4.0, 1.5, -0.5],
                        [1.5, 2.0, 0.3],
                        [-0.5, 0.3, 9.0]])
        d = HRP.get_correlation_distance(cov)
        assert np.allclose(d, d.T)

    def test_every_distance_is_between_zero_and_one(self):
        rng = np.random.default_rng(7)
        x = rng.normal(size=(40, 5))
        cov = np.cov(x, rowvar=False)
        d = HRP.get_correlation_distance(cov)
        assert np.all(d >= 0.0) and np.all(d <= 1.0)

    def test_a_numerically_impossible_correlation_is_clipped(self):
        """cov/(s_i*s_j) can exceed 1 in floating point; the clip keeps sqrt real."""
        cov = np.array([[1.0, 1.5], [1.5, 1.0]])
        d = HRP.get_correlation_distance(cov)
        assert np.all(np.isfinite(d))
        assert np.all(d >= 0.0)

    def test_a_zero_variance_asset_does_not_produce_nan(self):
        """A flat series has std 0; the 1e-8 floor has to stop the division."""
        cov = np.array([[0.0, 0.0], [0.0, 4.0]])
        d = HRP.get_correlation_distance(cov)
        assert np.all(np.isfinite(d))

    def test_higher_correlation_means_lower_distance(self):
        lo = HRP.get_correlation_distance(np.array([[1.0, 0.2], [0.2, 1.0]]))
        hi = HRP.get_correlation_distance(np.array([[1.0, 0.9], [0.9, 1.0]]))
        assert lo[0, 1] > hi[0, 1]

    def test_a_single_asset_has_zero_distance(self):
        d = HRP.get_correlation_distance(np.array([[2.5]]))
        assert d.shape == (1, 1)
        assert d[0, 0] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# allocate_weights — degenerate inputs
# ---------------------------------------------------------------------------

class TestDegenerateInputs:
    def test_none_returns_an_empty_dict(self):
        assert HRP.allocate_weights(None) == {}

    def test_an_empty_frame_returns_an_empty_dict(self):
        assert HRP.allocate_weights(pd.DataFrame()) == {}

    def test_a_frame_with_no_columns_returns_an_empty_dict(self):
        assert HRP.allocate_weights(pd.DataFrame(index=range(5))) == {}

    @pytest.mark.parametrize("cols", [1, 2, 3])
    def test_a_frame_with_columns_but_no_rows_returns_an_empty_dict(self, cols):
        """No observations means no variance, and a NaN weight is worse than none.

        Without the `empty` guard these fall through to `cov()` of an empty
        series, which is NaN, and come back as `{'A': nan}`.
        """
        df = pd.DataFrame({c: [] for c in "ABC"[:cols]})
        assert HRP.allocate_weights(df) == {}

    def test_a_single_asset_gets_all_of_the_weight(self):
        df = pd.DataFrame({"A": [1.0, -1.0, 2.0, -2.0]})
        assert HRP.allocate_weights(df) == {"A": 1.0}

    def test_a_single_flat_asset_still_gets_all_of_the_weight(self):
        """One asset is one asset — there is nothing to spread the weight over."""
        assert HRP.allocate_weights(pd.DataFrame({"A": [0.0, 0.0, 0.0]})) == {"A": 1.0}


# ---------------------------------------------------------------------------
# allocate_weights — the inverse-variance split
# ---------------------------------------------------------------------------

class TestInverseVarianceSplit:
    def test_the_lower_variance_asset_gets_more_weight(self):
        w = HRP.allocate_weights(_ab())
        assert w["A"] > w["B"]

    def test_a_fourfold_variance_ratio_splits_four_to_one(self):
        """var(B) == 4 * var(A), so the inverse-variance split is exactly 0.8/0.2."""
        w = HRP.allocate_weights(_ab())
        assert w["A"] == pytest.approx(0.8)
        assert w["B"] == pytest.approx(0.2)

    def test_equal_variance_is_an_even_split(self):
        df = pd.DataFrame({"A": [1.0, -1.0, 1.0], "B": [1.0, 1.0, -1.0]})
        w = HRP.allocate_weights(df)
        assert w["A"] == pytest.approx(w["B"])

    def test_weights_always_sum_to_one(self):
        rng = np.random.default_rng(11)
        df = pd.DataFrame(rng.normal(size=(200, 6)), columns=list("ABCDEF"))
        assert sum(HRP.allocate_weights(df).values()) == pytest.approx(1.0, abs=1e-3)

    def test_no_weight_is_negative(self):
        rng = np.random.default_rng(12)
        df = pd.DataFrame(rng.normal(size=(200, 6)), columns=list("ABCDEF"))
        assert all(v >= 0.0 for v in HRP.allocate_weights(df).values())

    def test_every_column_gets_a_key(self):
        w = HRP.allocate_weights(_ab())
        assert set(w) == {"A", "B"}

    def test_weights_are_rounded_to_four_places(self):
        rng = np.random.default_rng(13)
        df = pd.DataFrame(rng.normal(size=(300, 7)), columns=list("ABCDEFG"))
        for v in HRP.allocate_weights(df).values():
            assert v == round(v, 4)


# ---------------------------------------------------------------------------
# The flat-series defect
# ---------------------------------------------------------------------------

class TestFlatSeries:
    def test_a_flat_series_does_not_take_the_allocation(self):
        """A series with no variance has no measurable risk to size against.

        Flooring its variance at 1e-6 gave it an inverse variance of a million,
        which is ~100% of the portfolio — the flat asset, the one the data says
        least about, took the whole allocation. In a backtest that is exactly
        what a symbol that never traded looks like.
        """
        df = pd.DataFrame({
            "MOVES": [1.0, -1.0, 1.0, -1.0],
            "FLAT": [0.0, 0.0, 0.0, 0.0],
        })
        w = HRP.allocate_weights(df)
        assert w["FLAT"] == pytest.approx(0.0)
        assert w["MOVES"] == pytest.approx(1.0)

    def test_the_flat_series_still_appears_in_the_output(self):
        """It is excluded from the weight, not dropped from the result."""
        df = pd.DataFrame({"MOVES": [1.0, -1.0], "FLAT": [5.0, 5.0]})
        assert set(HRP.allocate_weights(df)) == {"MOVES", "FLAT"}

    def test_several_flat_series_still_sum_to_one(self):
        df = pd.DataFrame({
            "MOVES": [1.0, -1.0, 1.0, -1.0],
            "FLAT1": [0.0, 0.0, 0.0, 0.0],
            "FLAT2": [3.0, 3.0, 3.0, 3.0],
        })
        w = HRP.allocate_weights(df)
        assert w["MOVES"] == pytest.approx(1.0)
        assert sum(w.values()) == pytest.approx(1.0)

    def test_when_every_series_is_flat_the_weights_are_even(self):
        """No risk signal at all — fall back to equal weight rather than dividing by zero."""
        df = pd.DataFrame({"A": [1.0, 1.0, 1.0], "B": [2.0, 2.0, 2.0]})
        w = HRP.allocate_weights(df)
        assert w == {"A": 0.5, "B": 0.5}

    def test_a_near_flat_series_is_still_treated_as_real_risk(self):
        """The exclusion is for zero variance, not for low variance."""
        df = pd.DataFrame({
            "MOVES": [1.0, -1.0, 1.0, -1.0],
            "SMALL": [1e-4, -1e-4, 1e-4, -1e-4],
        })
        w = HRP.allocate_weights(df)
        assert w["SMALL"] == pytest.approx(1.0)
        assert w["MOVES"] == pytest.approx(0.0, abs=1e-4)


# ---------------------------------------------------------------------------
# What the module does *not* do — pinned so the gap is visible
# ---------------------------------------------------------------------------

class TestCorrelationIsIgnored:
    def test_two_perfectly_correlated_assets_are_not_diversified(self):
        """Inverse variance cannot see correlation; HRP exists precisely to fix that.

        A and B are the same series repeated, so a risk allocator should split
        them — instead each gets a weight proportional only to its variance.
        """
        a = np.array([1.0, -1.0, 2.0, -2.0, 3.0, -3.0])
        df = pd.DataFrame({"A": a, "B": a.copy()})
        w = HRP.allocate_weights(df)
        assert w["A"] == pytest.approx(0.5)
        assert w["B"] == pytest.approx(0.5)

    def test_the_correlation_distance_helper_is_not_used_by_the_allocator(self):
        """Pin that the one HRP-specific primitive is not wired in.

        If real HRP is ever implemented, this test is the thing that should
        start failing.
        """
        import inspect

        import jarvis.risk.hrp_allocator as mod
        src = inspect.getsource(mod.HierarchicalRiskParityAllocator.allocate_weights)
        assert "get_correlation_distance" not in src


class TestTrueHRPAllocation:
    def test_hrp_weights_sum_to_one(self):
        rng = np.random.default_rng(42)
        df = pd.DataFrame(rng.normal(size=(100, 4)), columns=list("ABCD"))
        weights = HRP.allocate_hrp_weights(df)
        assert sum(weights.values()) == pytest.approx(1.0, abs=1e-3)

    def test_hrp_diversifies_correlated_cluster(self):
        # A and B are highly correlated (cluster 1), C is independent (cluster 2)
        rng = np.random.default_rng(42)
        base = rng.normal(size=100)
        a = base + rng.normal(scale=0.1, size=100)
        b = base + rng.normal(scale=0.1, size=100)
        c = rng.normal(size=100)
        df = pd.DataFrame({"A": a, "B": b, "C": c})
        w = HRP.allocate_hrp_weights(df)
        # HRP allocates more weight to independent asset C than to individual assets A or B
        assert w["C"] > w["A"]
        assert w["C"] > w["B"]
        assert (w["A"] + w["B"] + w["C"]) == pytest.approx(1.0, abs=1e-3)
