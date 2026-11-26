# Path: MarioBot/CORE/STRATEGY/_markov_thresholds.py
# Role: Адаптивный подбор порогов режимов Маркова (3-кластерный GMM EM и квантильный метод из notes2.md)

from __future__ import annotations

import math
from typing import Sequence, Tuple
import numpy as np


class AdaptiveThresholdFinder:
    """
    Без зависимости от sklearn/scipy вычисляет адаптивные пороги (bull_thresh, bear_thresh)
    строго на скользящем окне прошлых 20-дневных доходностей (без look-ahead):
    - 'gmm_3state': 1D Gaussian Mixture Model (EM-алгоритм на 3 кластера Bear/Sideways/Bull).
    - 'quantile': эмпирические квантили распределения доходностей.
    """

    @staticmethod
    def find_gmm_thresholds(
        returns: Sequence[float],
        min_thresh_ratio: float = 0.005,
        max_thresh_ratio: float = 0.25,
        max_iter: int = 25,
    ) -> Tuple[float, float]:
        arr = np.asarray(returns, dtype=np.float64)
        if arr.shape[0] < 30:
            return 0.05, 0.05

        std_all = float(np.std(arr))
        if std_all <= 1e-8:
            return 0.05, 0.05

        # Детерминированная инициализация 3 гауссовых компонент по перцентилям
        means = np.percentile(arr, [15.0, 50.0, 85.0]).astype(np.float64)
        vars_ = np.full(3, max(std_all * std_all * 0.5, 1e-6), dtype=np.float64)
        weights = np.full(3, 1.0 / 3.0, dtype=np.float64)
        inv_sqrt_2pi = 1.0 / math.sqrt(2.0 * math.pi)

        resp = np.empty((arr.shape[0], 3), dtype=np.float64)
        for _ in range(max_iter):
            # E-step
            for k in range(3):
                v = max(float(vars_[k]), 1e-8)
                diff = arr - means[k]
                pdf = (inv_sqrt_2pi / math.sqrt(v)) * np.exp(-0.5 * (diff * diff) / v)
                resp[:, k] = weights[k] * pdf

            row_sums = np.sum(resp, axis=1, keepdims=True)
            row_sums = np.where(row_sums > 1e-15, row_sums, 1e-15)
            resp /= row_sums

            # M-step
            nk = np.sum(resp, axis=0)
            for k in range(3):
                if nk[k] < 2.0:
                    continue
                weights[k] = nk[k] / float(arr.shape[0])
                means[k] = float(np.sum(resp[:, k] * arr) / nk[k])
                diff = arr - means[k]
                vars_[k] = max(float(np.sum(resp[:, k] * (diff * diff)) / nk[k]), 1e-7)

        labels = np.argmax(resp, axis=1)
        order = np.argsort(means)
        bear_vals = arr[labels == order[0]]
        side_vals = arr[labels == order[1]]
        bull_vals = arr[labels == order[2]]

        if bear_vals.shape[0] < 3 or side_vals.shape[0] < 3 or bull_vals.shape[0] < 3:
            return AdaptiveThresholdFinder.find_quantile_thresholds(returns)

        raw_bull = float(np.min(bull_vals) + np.max(side_vals)) * 0.5
        raw_bear_signed = float(np.max(bear_vals) + np.min(side_vals)) * 0.5

        bull_thresh = max(min_thresh_ratio, min(max_thresh_ratio, raw_bull))
        bear_thresh = max(min_thresh_ratio, min(max_thresh_ratio, abs(min(raw_bear_signed, -min_thresh_ratio))))
        return bull_thresh, bear_thresh

    @staticmethod
    def find_quantile_thresholds(
        returns: Sequence[float],
        tail_quantile_ratio: float = 0.30,
        min_thresh_ratio: float = 0.005,
        max_thresh_ratio: float = 0.25,
    ) -> Tuple[float, float]:
        arr = np.asarray(returns, dtype=np.float64)
        if arr.shape[0] < 20:
            return 0.05, 0.05
        q_low = float(np.quantile(arr, tail_quantile_ratio))
        q_high = float(np.quantile(arr, 1.0 - tail_quantile_ratio))
        bull_thresh = max(min_thresh_ratio, min(max_thresh_ratio, q_high))
        bear_thresh = max(min_thresh_ratio, min(max_thresh_ratio, abs(min(q_low, -min_thresh_ratio))))
        return bull_thresh, bear_thresh
