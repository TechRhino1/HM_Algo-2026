"""
HM Algo 2.0 — Portfolio Risk Allocator.

WHAT THIS ACTUALLY COMPUTES
---------------------------
Inverse-variance weighting, *not* Hierarchical Risk Parity. The class is still
named ``HierarchicalRiskParityAllocator`` because ``tools/run_3month_backtest.py``
imports it under that name, and the older reports and README call it "HRP", but
the algorithm has never been implemented here: there is no clustering, no
quasi-diagonalisation and no recursive bisection, and ``get_correlation_distance``
— the one HRP-specific primitive — has never been called by anything.

The practical consequence is that **correlation is invisible to this allocator**.
Two assets that move together each receive a full inverse-variance weight, so the
portfolio can be far more concentrated than the weights suggest. That is the
problem HRP exists to solve; see ``tests/test_hrp_allocator.py`` for the pinned
behaviour.
"""
import numpy as np
import pandas as pd
from typing import Dict

class HierarchicalRiskParityAllocator:
    """Inverse-variance portfolio allocator.

    Named for the HRP algorithm it does not (yet) implement — see the module
    docstring. Kept under this name for import compatibility.
    """

    @staticmethod
    def get_correlation_distance(cov: np.ndarray) -> np.ndarray:
        """Computes correlation distance matrix d_ij = sqrt(0.5 * (1 - rho_ij))."""
        std = np.sqrt(np.diag(cov))
        std[std == 0] = 1e-8
        corr = cov / np.outer(std, std)
        corr = np.clip(corr, -1.0, 1.0)
        dist = np.sqrt(0.5 * (1.0 - corr))
        np.fill_diagonal(dist, 0.0)
        return dist

    @classmethod
    def allocate_weights(cls, returns_df: pd.DataFrame) -> Dict[str, float]:
        """Inverse-variance portfolio weights for an asset returns DataFrame.

        Weight is proportional to ``1 / variance``. Assets with no measurable
        variance get zero rather than the whole allocation — see the note below.
        """
        if returns_df is None or returns_df.empty or returns_df.shape[1] == 1:
            if returns_df is not None and not returns_df.empty:
                return {returns_df.columns[0]: 1.0}
            return {}

        cov = returns_df.cov().values
        assets = list(returns_df.columns)
        n = len(assets)

        if n == 0:
            return {}

        # 1. Inverse variance weights
        # ``np.diag`` returns a read-only view in modern numpy, so the floor
        # assignment below raised "assignment destination is read-only".
        variances = np.diag(cov).copy()

        # A flat series has no measurable risk to size a position against. The
        # old code floored its variance at 1e-6, which is an inverse variance of
        # a million — so the one asset the data says least about took the entire
        # allocation (and in a backtest, "flat" is exactly what a symbol that
        # never traded looks like). Mark it degenerate and give it no weight.
        degenerate = variances <= 1e-12
        variances[degenerate] = 1.0            # placeholder; zeroed below
        variances[~degenerate & (variances <= 0)] = 1e-6   # numerical noise

        inv_var = 1.0 / variances
        inv_var[degenerate] = 0.0

        total = np.sum(inv_var)
        # Nothing measurable at all: equal weight is the neutral answer, and it
        # keeps the weights summing to 1 instead of dividing by zero.
        weights = np.full(n, 1.0 / n) if total <= 0 else inv_var / total

        # 2. Return normalized asset weights dictionary
        return {assets[i]: round(float(weights[i]), 4) for i in range(n)}

    @staticmethod
    def get_quasi_diag(link: np.ndarray) -> list[int]:
        """Sort clustered items by hierarchical tree order."""
        link = link.astype(int)
        sort_ix = pd.Series([link[-1, 0], link[-1, 1]])
        num_items = link[-1, 3]
        while sort_ix.max() >= num_items:
            sort_ix.index = range(0, sort_ix.shape[0] * 2, 2)
            df0 = sort_ix[sort_ix >= num_items]
            i = df0.index
            j = df0.values - num_items
            sort_ix[i] = link[j, 0]
            df0 = pd.Series(link[j, 1], index=i + 1)
            sort_ix = pd.concat([sort_ix, df0]).sort_index()
            sort_ix.index = range(sort_ix.shape[0])
        return sort_ix.tolist()

    @classmethod
    def get_cluster_var(cls, cov: np.ndarray, c_items: list[int]) -> float:
        """Computes cluster variance for an inverse-variance weighted sub-portfolio."""
        cov_sub = cov[np.ix_(c_items, c_items)]
        variances = np.diag(cov_sub).copy()
        degenerate = variances <= 1e-12
        variances[degenerate] = 1.0
        variances[~degenerate & (variances <= 0)] = 1e-6
        inv_var = 1.0 / variances
        inv_var[degenerate] = 0.0
        total = np.sum(inv_var)
        w = np.full(len(c_items), 1.0 / len(c_items)) if total <= 0 else inv_var / total
        w = w.reshape(-1, 1)
        c_var = np.dot(np.dot(w.T, cov_sub), w)[0, 0]
        return float(c_var)

    @classmethod
    def get_rec_bisection(cls, cov: np.ndarray, sort_ix: list[int]) -> np.ndarray:
        """Recursive bisection to compute HRP weights."""
        w = pd.Series(1.0, index=sort_ix)
        c_items = [sort_ix]
        while len(c_items) > 0:
            c_items = [i[j:k] for i in c_items for j, k in ((0, len(i) // 2), (len(i) // 2, len(i))) if len(i) > 1]
            for i in range(0, len(c_items), 2):
                c_items0 = c_items[i]
                c_items1 = c_items[i + 1]
                var0 = cls.get_cluster_var(cov, c_items0)
                var1 = cls.get_cluster_var(cov, c_items1)
                alpha = 1.0 - var0 / (var0 + var1 + 1e-12)
                w[c_items0] *= alpha
                w[c_items1] *= (1.0 - alpha)
        return w.values

    @classmethod
    def allocate_hrp_weights(cls, returns_df: pd.DataFrame) -> Dict[str, float]:
        """True Hierarchical Risk Parity (HRP) portfolio allocator (Marcos Lopez de Prado 2016).
        Performs tree clustering, quasi-diagonalization, and recursive bisection.
        Accounts for cross-asset correlation rather than ignoring it.
        """
        if returns_df is None or returns_df.empty or returns_df.shape[1] == 1:
            if returns_df is not None and not returns_df.empty:
                return {returns_df.columns[0]: 1.0}
            return {}

        import scipy.cluster.hierarchy as sch
        from scipy.spatial.distance import squareform

        cov = returns_df.cov().values
        assets = list(returns_df.columns)
        n = len(assets)
        if n == 0:
            return {}

        # 1. Tree clustering using correlation distance
        dist_mat = cls.get_correlation_distance(cov)
        condensed_dist = squareform(dist_mat, checks=False)
        link = sch.linkage(condensed_dist, method="single")

        # 2. Quasi-diagonalization
        sort_ix = cls.get_quasi_diag(link)

        # 3. Recursive bisection
        hrp_weights = cls.get_rec_bisection(cov, sort_ix)

        # Normalize and construct dict
        total = np.sum(hrp_weights)
        if total > 0:
            hrp_weights = hrp_weights / total
        else:
            hrp_weights = np.full(n, 1.0 / n)

        return {assets[sort_ix[i]]: round(float(hrp_weights[i]), 4) for i in range(n)}
