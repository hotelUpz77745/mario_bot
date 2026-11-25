==============================================================




 https://www.youtube.com/watch?v=P1_J8IeDfdE сделай подробный конспект этого видео а также: класс на пайтон системы оценки вероятности завтрашнего тренда. Веса они же метки из видео задаются при инициализации и по умолчанию равны 5% класс на пайтон системы подсчета сайзинга (например в процентах) на основании вероятности разниц между сегодняшним и завтрашним тестах класс на пайтон который автоматически, принимая актив и его свечные данные сможет автоматически вычислить для текущего дня оптимальные веса они же метки из видео (для определения тренда).




Подробный конспект видео и готовые Python-классы для реализации модели режимов Маркова, расчета размера позиции и автоматического подбора пороговых меток.

Конспект видео: «Метод хедж-фондов» (Модель режимов Маркова)

В основе ролика лежит подход квантов (вдохновленный материалами специалиста хедж-фонда Роквана Н), который заменяет субъективный технический анализ, линии тренда и индикаторы математической оценкой состояний рынка (0:07). Система состоит из 10 ключевых элементов (1:03):
1. Оцифровка ощущений (Состояния / Режимы): Кванты не работают с эмоциями (1:27). Рынок делится на 3 четких состояния на основе накопленной доходности за последние 20 торговых дней (1:46):
	• Бычий режим (Bull): доходность \(\ge +5\%\) (1:53).
	• Медвежий режим (Bear): доходность \(\le -5\%\) (2:07).
	• Боковой режим (Sideways): доходность строго между \(-5\%\) и \(+5\%\) (2:15).
2. Историческая разметка: Алгоритм проходит по всей доступной истории актива (начиная с 20-го дня) и присваивает каждому дню соответствующую метку состояния (2:23).
3. Свойство Маркова: Будущее состояние рынка зависит исключительно от его текущего состояния (сегодняшнего дня), а не от того, как именно рынок пришел в эту точку (2:42).
4. Переходная матрица (Матрица хедж-фондов 3×3): Подсчитываются все исторические переходы из одного состояния в другое (например, сколько раз после «бычьего» дня следовал «боковой») (3:26). Количества переводятся в вероятности (сумма по каждой строке равна 100%) (3:51). Строки — сегодня, столбцы — завтра (3:58).
5. Устойчивость («Липкость»): Главная диагональ матрицы (Бычий \(\rightarrow \) Бычий, Медвежий \(\rightarrow \) Медвежий, Боковой \(\rightarrow \) Боковой) показывает инерцию тренда (4:17). Высокая «липкость» (например, 80%) означает, что текущий режим с высокой вероятностью сохранится завтра (4:31).
6. Многодневные прогнозы (Возведение матрицы в степень): Чтобы узнать вероятность режимов через \(N\) дней, переходная матрица перемножается сама на себя \(N\) раз (\(M^{2}\) для прогноза на 2 дня, \(M^{3}\) — на 3 дня и т.д.) (5:23).
7. Стационарное распределение: При возведении матрицы в большую степень (например, прогноз на 28 дней) вероятности выравниваются и сходятся к базовым историческим средним (6:15). Сигнал полностью затухает — делать точечные прогнозы на такой горизонт бессмысленно (6:31).
8. Генерация сигнала и управление размером позиции: Сигнал вычисляется по простой формуле:
\(\text{Сигнал}=P(\text{Бычий завтра})-P(\text{Медвежий завтра})\) (6:51)
	• Положительное значение — сигнал в Лонг, отрицательное — в Шорт (7:12).
	• Модуль разницы определяет силу уверенности и размер позиции (сайзинг) (6:56).
9. Walk-Forward бэктестинг: Чтобы избежать «подглядывания в будущее» (overfitting), матрица не строится один раз на всей истории, а пересчитывается заново на каждый исторический день с использованием только тех данных, которые были доступны до этого дня (7:48).
10. Скрытая марковская модель (HMM): Пороги \(\pm 5\%\) — это субъективный выбор человека (8:22). HMM анализирует «сырые» данные (доходности, волатильность) без ручных меток и сама кластеризует рынок на 3 скрытых режима (8:31). Совпадение сигналов классической матрицы и HMM служит главным подтверждением для входа в сделку (9:10).

Реализация системы на Python

Для работы кода понадобятся стандартные библиотеки: numpy, pandas и (для 3-го класса) scipy или scikit-learn (pip install numpy pandas scipy scikit-learn).

1. Класс системы оценки вероятности завтрашнего тренда

Рассчитывает 20-дневную доходность, размечает режимы, строит матрицу переходов \(3\times3\), оценивает «липкость» и выдает вероятности на завтра (или на \(N\) дней вперед) (1:46).
python
import numpy as np
import pandas as pd


class MarkovTrendPredictor:
    """Система оценки вероятности завтрашнего тренда на основе цепей Маркова."""

    STATES = ['Bear', 'Sideways', 'Bull']

    def __init__(
        self,
        bull_threshold: float = 0.05,
        bear_threshold: float = -0.05,
        lookback: int = 20,
    ):
        """
        :param bull_threshold: Порог для бычьего режима (по умолчанию +5% = 0.05)
        :param bear_threshold: Порог для медвежьего режима (по умолчанию -5% = -0.05)
        :param lookback: Окно расчета доходности в днях (по умолчанию 20)
        """
        self.bull_threshold = bull_threshold
        self.bear_threshold = bear_threshold
        self.lookback = lookback
        self.transition_matrix: pd.DataFrame | None = None
        self.current_state: str | None = None

    def _classify_return(self, ret: float) -> str:
        if ret >= self.bull_threshold:
            return 'Bull'
        elif ret <= self.bear_threshold:
            return 'Bear'
        return 'Sideways'

    def fit(self, close_prices: pd.Series) -> 'MarkovTrendPredictor':
        """Обучение матрицы переходов на исторических ценах закрытия."""
        # Расчет доходности за lookback дней (как сумма дневных доходностей из видео или % изменения)
        # Используем процентное изменение за период (или .pct_change().rolling(lookback).sum())
        rolling_ret = close_prices.pct_change(self.lookback).dropna()

        if len(rolling_ret) < 2:
            raise ValueError('Недостаточно данных для построения матрицы.')

        states = rolling_ret.apply(self._classify_return)
        self.current_state = states.iloc[-1]

        # Подсчет переходов: вчера (t) -> сегодня (t+1)
        transitions = pd.crosstab(
            states.shift(1).dropna(), states.iloc[1:], normalize='index'
        )

        # Гарантируем наличие всех 3x3 состояний, даже если каких-то не было в истории
        self.transition_matrix = transitions.reindex(
            index=self.STATES, columns=self.STATES, fill_value=0.0
        )
        return self

    def predict_next(self, days_ahead: int = 1) -> dict[str, float]:
        """Возвращает словарь вероятностей состояний через `days_ahead` дней."""
        if self.transition_matrix is None or self.current_state is None:
            raise RuntimeError('Сначала вызовите метод fit().')

        # Возводим матрицу в степень N для прогноза на N дней вперед
        matrix_n = np.linalg.matrix_power(
            self.transition_matrix.values, days_ahead
        )
        df_matrix_n = pd.DataFrame(
            matrix_n, index=self.STATES, columns=self.STATES
        )

        probs = df_matrix_n.loc[self.current_state].to_dict()
        return {k: round(float(v), 4) for k, v in probs.items()}

    def get_stickiness(self) -> dict[str, float]:
        """Возвращает 'липкость' (главную диагональ) для каждого режима."""
        if self.transition_matrix is None:
            raise RuntimeError('Сначала вызовите метод fit().')
        return {
            state: round(float(self.transition_matrix.loc[state, state]), 4)
            for state in self.STATES
        }
Используйте код с осторожностью.

2. Класс системы подсчета сайзинга позиции

Реализует 8-й элемент из видео: вычитает вероятность медвежьего исхода из вероятности бычьего, учитывает направление (Лонг/Шорт), опционально корректирует объем на «липкость» текущего режима и переводит итог в процент от депозита (6:51).
python
class MarkovPositionSizer:
    """Система расчета размера позиции (в %) на основе разницы вероятностей режимов."""

    def __init__(
        self,
        max_allocation_pct: float = 100.0,
        min_edge_threshold: float = 0.10,
        use_stickiness_weight: bool = False,
    ):
        """
        :param max_allocation_pct: Максимально допустимый размер позиции в % от капитала
        :param min_edge_threshold: Минимальная разница вероятностей для входа (например, 0.10 = 10%)
        :param use_stickiness_weight: Умножать ли сигнал на устойчивость текущего режима
        """
        self.max_allocation_pct = max_allocation_pct
        self.min_edge_threshold = min_edge_threshold
        self.use_stickiness_weight = use_stickiness_weight

    def calculate_sizing(
        self,
        tomorrow_probs: dict[str, float],
        current_state: str | None = None,
        stickiness: dict[str, float] | None = None,
    ) -> dict[str, float | str]:
        """
        Рассчитывает направление и размер позиции в процентах.
        :param tomorrow_probs: Словарь {'Bull': float, 'Bear': float, 'Sideways': float}
        """
        p_bull = tomorrow_probs.get('Bull', 0.0)
        p_bear = tomorrow_probs.get('Bear', 0.0)

        # Базовая формула из видео: P(Bull) - P(Bear)
        raw_edge = p_bull - p_bear

        # Опциональная корректировка на устойчивость ("липкость") текущего состояния
        if (
            self.use_stickiness_weight
            and current_state
            and stickiness
            and current_state != 'Sideways'
        ):
            state_stickiness = stickiness.get(current_state, 1.0)
            effective_edge = raw_edge * state_stickiness
        else:
            effective_edge = raw_edge

        # Фильтр шума (если перевес слишком слабый, остаемся вне рынка)
        if abs(effective_edge) < self.min_edge_threshold:
            return {
                'direction': 'FLAT',
                'raw_edge_pct': round(raw_edge * 100, 2),
                'position_size_pct': 0.0,
            }

        direction = 'LONG' if effective_edge > 0 else 'SHORT'
        # Масштабируем уверенность в процент от выделенного лимита капитала
        size_pct = min(
            abs(effective_edge) * self.max_allocation_pct,
            self.max_allocation_pct,
        )

        return {
            'direction': direction,
            'raw_edge_pct': round(raw_edge * 100, 2),
            'position_size_pct': round(size_pct, 2),
        }
Используйте код с осторожностью.

3. Класс автоматического вычисления оптимальных весов (пороговых меток)

В 10-м элементе видео автор отмечает, что ручные пороги \(\pm 5\%\) — это слабое звено, так как у S&P 500 и у Bitcoin разная волатильность (8:22). Этот класс принимает свечные данные актива и автоматически находит оптимальные пороги bull_threshold и bear_threshold двумя методами (на выбор):
1. Через кластеризацию (K-Means / GMM — аналог Скрытой Марковской Модели): разбивает 20-дневные доходности актива на 3 естественных режима без участия человека и находит границы между ними.
2. Через оптимизацию (Walk-Forward / Max Stickiness & Edge): подбирает такие пороги на недавнем окне истории, которые максимизируют предсказуемость (липкость диагонали матрицы) и доходность сигнала к текущему дню.
python
from scipy.optimize import minimize
from sklearn.mixture import GaussianMixture


class OptimalThresholdFinder:
    """Автоматически вычисляет оптимальные пороги (метки) режимов для конкретного актива."""

    def __init__(
        self,
        lookback: int = 20,
        method: str = 'gmm',
        calibration_window: int = 500,
    ):
        """
        :param lookback: Окно доходности (по умолчанию 20 дней)
        :param method: 'gmm' (поиск естественных кластеров как в HMM) или 'grid' (оптимизация матричного перевеса)
        :param calibration_window: Сколько последних свечей использовать для калибровки под текущий день
        """
        self.lookback = lookback
        self.method = method
        self.calibration_window = calibration_window

    def find_optimal_weights(self, df_candles: pd.DataFrame) -> dict[str, float]:
        """
        Принимает DataFrame со свечами (должна быть колонка 'close' или 'Close').
        Возвращает оптимальные пороги для текущего дня.
        """
        close_col = 'close' if 'close' in df_candles.columns else 'Close'
        if close_col not in df_candles.columns:
            raise ValueError("DataFrame должен содержать колонку 'close' или 'Close'.")

        prices = df_candles[close_col].tail(
            self.calibration_window + self.lookback
        )
        rolling_ret = prices.pct_change(self.lookback).dropna().values

        if len(rolling_ret) < 50:
            raise ValueError('Слишком мало свечных данных для калибровки весов.')

        if self.method == 'gmm':
            return self._fit_gmm_thresholds(rolling_ret)
        elif self.method == 'grid':
            return self._fit_grid_thresholds(prices)
        else:
            raise ValueError("Метод должен быть 'gmm' или 'grid'.")

    def _fit_gmm_thresholds(self, returns: np.ndarray) -> dict[str, float]:
        """
        Разделяет доходности на 3 гауссовских режима (аналог HMM из 10-го пункта видео)
        и находит естественные границы переключения между режимами.
        """
        X = returns.reshape(-1, 1)
        gmm = GaussianMixture(n_components=3, random_state=42, n_init=5)
        labels = gmm.fit_predict(X)

        # Сортируем кластеры по среднему значению доходности: 0 -> Bear, 1 -> Sideways, 2 -> Bull
        means = [returns[labels == i].mean() for i in range(3)]
        sorted_idx = np.argsort(means)

        bear_cluster = returns[labels == sorted_idx[0]]
        sideways_cluster = returns[labels == sorted_idx[1]]
        bull_cluster = returns[labels == sorted_idx[2]]

        # Граница между боковиком и бычьим рынком
        bull_thresh = (sideways_cluster.max() + bull_cluster.min()) / 2
        # Граница между боковиком и медвежьим рынком
        bear_thresh = (sideways_cluster.min() + bear_cluster.max()) / 2

        # Защита от вырожденных значений (минимум 0.5% движения)
        bull_thresh = max(float(bull_thresh), 0.005)
        bear_thresh = min(float(bear_thresh), -0.005)

        return {
            'bull_threshold': round(bull_thresh, 4),
            'bear_threshold': round(bear_thresh, 4),
            'bull_threshold_pct': round(bull_thresh * 100, 2),
            'bear_threshold_pct': round(bear_thresh * 100, 2),
        }

    def _fit_grid_thresholds(self, prices: pd.Series) -> dict[str, float]:
        """
        Перебирает сетку порогов на последних свечах и ищет веса,
        которые дают максимальный коэффициент Шарпа стратегии P(Bull) - P(Bear).
        """
        best_score = -np.inf
        best_weights = (0.05, -0.05)

        # Диапазон поиска порогов от 1% до 15% с шагом 0.5%
        candidates = np.arange(0.01, 0.155, 0.005)
        daily_ret = prices.pct_change().fillna(0)
        rolling_ret = prices.pct_change(self.lookback)

        for thresh in candidates:
            # Быстрая векторизованная симуляция сигнала
            states = np.where(
                rolling_ret >= thresh, 1, np.where(rolling_ret <= -thresh, -1, 0)
            )
            valid_mask = ~rolling_ret.isna()
            st = pd.Series(states[valid_mask], index=prices.index[valid_mask])

            if len(st.unique()) < 3:
                continue

            # Считаем матрицу переходов
            trans = pd.crosstab(st.shift(1).dropna(), st.iloc[1:], normalize='index')
            if not all(k in trans.index and k in trans.columns for k in [-1, 0, 1]):
                continue

            # Сигнал для каждого состояния: P(Bull) - P(Bear)
            edge_map = {s: trans.loc[s, 1] - trans.loc[s, -1] for s in [-1, 0, 1]}
            signals = st.map(edge_map).shift(1).fillna(0)

            strat_returns = signals * daily_ret.loc[signals.index]
            std = strat_returns.std()
            if std > 0:
                sharpe = (strat_returns.mean() / std) * np.sqrt(252)
                if sharpe > best_score:
                    best_score = sharpe
                    best_weights = (thresh, -thresh)

        return {
            'bull_threshold': round(float(best_weights[0]), 4),
            'bear_threshold': round(float(best_weights[1]), 4),
            'bull_threshold_pct': round(float(best_weights[0]) * 100, 2),
            'bear_threshold_pct': round(float(best_weights[1]) * 100, 2),
        }
Используйте код с осторожностью.

Пример связки всех трех классов в единый пайплайн

python
# Генерация тестовых свечных данных (замените на загрузку из yfinance / ccxt / CSV)
np.random.seed(42)
dates = pd.date_range('2024-01-01', periods=600, freq='D')
prices = 100 * np.exp(np.cumsum(np.random.normal(0.0005, 0.02, size=600)))
df = pd.DataFrame({'Date': dates, 'Close': prices})

# 1. Автоматически вычисляем оптимальные пороги для актива на текущий день
finder = OptimalThresholdFinder(lookback=20, method='gmm', calibration_window=500)
optimal_weights = finder.find_optimal_weights(df)
print('Оптимальные пороги:', optimal_weights)

# 2. Инициализируем модель Маркова с найденными весами (или оставляем 0.05 по умолчанию)
predictor = MarkovTrendPredictor(
    bull_threshold=optimal_weights['bull_threshold'],
    bear_threshold=optimal_weights['bear_threshold'],
    lookback=20,
)
predictor.fit(df['Close'])

tomorrow_probs = predictor.predict_next(days_ahead=1)
stickiness = predictor.get_stickiness()
print(f'Текущий режим: {predictor.current_state}')
print('Вероятности на завтра:', tomorrow_probs)

# 3. Рассчитываем размер позиции на основе разницы вероятностей
sizer = MarkovPositionSizer(max_allocation_pct=100.0, min_edge_threshold=0.10)
position = sizer.calculate_sizing(
    tomorrow_probs,
    current_state=predictor.current_state,
    stickiness=stickiness,
)
print('Итоговый сигнал и сайзинг:', position)
Используйте код с осторожностью.
Примечание: Приведенный код предназначен для образовательных и исследовательских целей. При тестировании на реальных рынках обязательно используйте Walk-Forward проверку, чтобы исключить утечку будущих данных при подборе порогов (8:03).



<!-- Хотите, чтобы я дополнил этот код:
• Классом для Walk-Forward бэктестинга (9-й элемент из видео) с расчетом кривой доходности и просадки
• Интеграцией с реальным источником данных (например, yfinance для акций или ccxt для криптовалют)
• Реализацией полноценной Скрытой Марковской Модели (hmmlearn) для двойного подтверждения сигналов -->