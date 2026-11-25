# Боевые стратегии ClavaTrading: механика, риск-менеджмент и сравнение (`$10,000 / 2x`)

В рабочей папке [`ClavaBot/CFG/strategies/`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/strategies) хранятся боевые паспорта стратегий (плюс **Боевой паспорт №3** для расширенного пула из 15 альтов со сложным процентом).

**Семейство Маркова (лидеры проекта):**
1. [`markov_pure_4h_gmm_conf_antichop_exit_sw_sl5_top15_r15.json`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/strategies/markov_pure_4h_gmm_conf_antichop_exit_sw_sl5_top15_r15.json) — **БОЕВОЙ ПАСПОРТ №3 (АБСОЛЮТНЫЙ ЛИДЕР ПО ROI И ЧАСТОТЕ СДЕЛОК)**: `exit_sw_sl5` на пуле **15 отобранных альтов** (`max_open_positions = 10`, `2x`, **`risk_per_trade_ratio = 0.015`** вместо `fixed_notional_usd`). Даёт **`~25` сделок/мес**, **`+$105,710 (+1,057%)`** за 5 лет (`Calmar 3.23`) и **`+$18,213 (+182.1%)`** за полгода (`PF 4.03`, `Calmar 14.35`).
2. [`markov_pure_4h_gmm_conf_antichop_atr_btc_sl5.json`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/strategies/markov_pure_4h_gmm_conf_antichop_atr_btc_sl5.json) — **ГЛАВНЫЙ ПРОД-ФЛАГМАН НА 6 МАЖОРАХ** (`antichop` + `ATR%` волатильностный сайзинг + сентимент `BTCUSDT` + `recal=1` + `5 × ATR` SL). **Все 6 монет в плюсе за 5 лет (включая `LTCUSDT +$1,549`)!**
3. [`markov_pure_4h_gmm_conf_antichop_exit_sw_sl5.json`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/strategies/markov_pure_4h_gmm_conf_antichop_exit_sw_sl5.json) — **ТАКТИЧЕСКАЯ БАЗА С ВЫХОДОМ В КЭШ В БОКОВИКЕ (`exit_on_sideways = true`)** на 6 монетах (`2022–2023 +$8,455`, лучшее дно `$8,294`, полгода `Calmar 10.50`).
4. [`markov_pure_4h_gmm_conf_antichop_shield.json`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/strategies/markov_pure_4h_gmm_conf_antichop_shield.json) — **МАКСИМАЛЬНАЯ ЗАЩИТА ПОРТФЕЛЯ** (всё из `atr_btc_sl5` + кросс-рыночный `Market Breadth < 3/6` + блок `LONG` при `BTC BEAR`).
5. [`markov_pure_4h_gmm_conf_antichop.json`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/strategies/markov_pure_4h_gmm_conf_antichop.json) — `antichop` без стопов (`block_in_sideways` + `ER >= 0.20`).
6. [`markov_pure_4h_gmm_conf.json`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/strategies/markov_pure_4h_gmm_conf.json) — базовая модель Маркова `15%` (исторический дефолт в [`app.json`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/app.json)).

### Как подключить Боевой паспорт №3 (`Top-15 + risk_per_trade_ratio = 1.5%`)
1. **Биржевые спецификации готовы:** в [`ClavaBot/CFG/app.json`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/app.json) (`symbol_specs`) уже занесены точные фильтры Binance Futures (`tick_size`, `step_size`, `min_qty`, `min_notional_usd`) для всех **15 рекомендованных монет** (`ADAUSDT`, `SOLUSDT`, `AVAXUSDT`, `DOGEUSDT`, `1000SHIBUSDT`, `HBARUSDT`, `SUIUSDT`, `UNIUSDT`, `XRPUSDT`, `BCHUSDT`, `DOTUSDT`, `BNBUSDT`, `ETHUSDT`, `LINKUSDT`, `XLMUSDT`) и `BTCUSDT` *(а также `LTCUSDT`, `TRXUSDT`, `TONUSDT`, `HYPEUSDT` для совместимости)*.
2. **Подключение по умолчанию:** в [`ClavaBot/CFG/app.json`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/app.json) измените поле:
   ```json
   "default_passport": "markov_pure_4h_gmm_conf_antichop_exit_sw_sl5_top15_r15.json"
   ```
   и запустите `python -m ClavaBot.main` — [`PositionSizer`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CORE/STRATEGY/position_sizer.py) автоматически переключится на расчёт лота от риска до стопа `5 × ATR`:
   $$\text{Notional} = \min\!\left(\frac{\text{Equity} \times 0.015 \times M_{\text{total}}}{5 \times \text{ATR}(14)} \times \text{Price},\;\text{FreeMargin} \times 2\right)$$
   а [`TradeEngineOrchestrator`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CORE/orchestrator.py) ограничит число одновременно открытых сделок `10` слотами (`max_open_positions = 10`).
3. **Прямой запуск из Python без смены `default_passport`:** `build_prod_runtime("markov_pure_4h_gmm_conf_antichop_exit_sw_sl5_top15_r15.json")`.
4. **Примечание по плечу при `5 × ATR` SL:** оптимально держать **`leverage = 2`** (верхний допустимый потолок `3x` даёт `MaxDD 76.7%`, а `4x` разрушает сложный процент до `MaxDD 93.6%`). Подробнее — в [`STRATEGIES_REPORT_2.md`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/STRATEGIES_REPORT_2.md).

**Семейство `EMA 7/25`:**
5. [`ema_cross_7_25_4h_markov_probe20_atr_sl5.json`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/strategies/ema_cross_7_25_4h_markov_probe20_atr_sl5.json) — **рекорд прибыли среди EMA** (`20% → 100%` + `ATR%` сайзинг + `5 × ATR SL`): **вывел даже 2025 год в плюс (`+$25.96`)!**
6. [`ema_cross_7_25_4h_markov_hybrid_sl5.json`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/strategies/ema_cross_7_25_4h_markov_hybrid_sl5.json) — **боевой гибрид** (мёртвый флэт `ADX < 15` заблокирован, `15 <= ADX < 21` → проба `20%`, на пробое `ADX >= 21` в прибыль → добор `100%` + `ATR%` + `BTCUSDT` сентимент + `5 × ATR SL`).
7. [`ema_cross_7_25_4h_markov_probe20.json`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/strategies/ema_cross_7_25_4h_markov_probe20.json) — базовый пробник `20% → 100%` без `ATR%` и без стопа.
8. [`ema_cross_7_25_4h_markov_adx20.json`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/strategies/ema_cross_7_25_4h_markov_adx20.json) — жёсткий фильтр `ADX(14) >= 20`.
9. [`ema_cross_7_25_4h_markov.json`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/strategies/ema_cross_7_25_4h_markov.json) — базовый микс `EMA 7/25` + асимметричный Марковский гейт.
10. [`ema_cross_7_25_4h.json`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/strategies/ema_cross_7_25_4h.json) — **чистая** `EMA 7/25` без фильтров.

---

## 1. Риск-менеджмент, стопы, тейки и условия симуляции

Все стратегии работают внутри единого движка [`TradeEngineOrchestrator`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CORE/orchestrator.py) и 1-минутного симулятора [`SimExecutionBroker`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBacktest/ENGINE/sim_broker.py):

* **Стартовый счёт и плечо:** общий портфель **`$10,000`**, кредитное плечо **`2x`** (запас маржи `$20,000`, чтобы все 6 монет входили независимо, без отсечения сделок по нехватке кэша).
* **График и инструменты:** свечи **4 часа (`4h`, UTC)**, **6 монет** (`ADAUSDT`, `SOLUSDT`, `XRPUSDT`, `DOGEUSDT`, `ETHUSDT`, `LTCUSDT`). В стратегиях с `MarketSentinel` дополнительно подаётся поток **4H свечей `BTCUSDT`** только как рыночный индикатор (без сделок по BTC).
* **Лимит позиций:** максимум **1 позиция на монету** (до `6` одновременно).
* **Исполнение и комиссии:** сигнал считается по закрытию 4H свечи, вход/выход/добор исполняются по `Open` следующей **1-минутной** свечи; комиссия **`0.05%`** вход + **`0.05%`** выход (`0.10%` круг), проскальзывание **`0.01%`** на сторону. Отдельно проверена инвариантность без подглядывания в будущее (полное совпадение сигналов на обрезанном горизонте vs полном датасете).

### Как работают Take-Profit и Stop-Loss ([`RiskStopTargetCalculator`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CORE/STRATEGY/risk_sl_tp_calculator.py))
* **Take-Profit (`tp_rr_ratio = null`):** фиксированных тейк-профитов **нет ни в одном из 10 паспортов**. Стратегии тренд-следящие: они не режут потолок прибыли и держат позицию до **противоположного сигнала с переворотом (`OPPOSITE_SIGNAL`)**. *(Технически движок умеет считать TP как `risk_dist * tp_rr_ratio`, но во всех боевых паспортах он отключён.)*
* **Stop-Loss:**
  * **6 базовых и промежуточных паспортов** (`ema_cross_7_25_4h*` без `_sl5`, `markov_pure_4h_gmm_conf`, `antichop`): `sl_anchor = "none"`, `sl_mode = "none"`, `soft_stop_enabled = false`. Жёсткого стопа нет — выход только по противоположному сигналу.
  * **4 финальных прод-паспорта** (`*_atr_btc_sl5`, `*_shield`, `*_probe20_atr_sl5`, `*_hybrid_sl5`): стоит **аварийный биржевой стоп `5 × ATR(14)`** (`sl_mode = "wide_atr"`, `atr_sl_mult = 5.0`, `sl_anchor = "origin_swing_extreme"`, `sl_buffer_ratio = null`):
    * При входе ставится ордер на уровне: `SL_LONG = Close - 5 × ATR(14)`, `SL_SHORT = Close + 5 × ATR(14)` (округляется по `tick_size` монеты).
    * В бэктесте триггер стопа проверяется **на каждой 1-минутной свече** (`Low <= SL` для LONG, `High >= SL` для SHORT, пессимистично `SL` раньше `TP`).
    * При закрытии по стопу (`STOP_LOSS`) срабатывает комиссия `0.05%` + проскальзывание `0.01%`. В моделях Маркова с `reentry_mode = "always"` при сохранении перевеса на следующих сутках возможен перезаход; в `EMA` сделка ждёт нового кроссовера или разворота.
    * При доборе `20% → 100%` позиция усредняется по средневзвешенной цене входа, а установленный при открытии `stop_loss` продолжает защищать весь объём.

---

## 2. Как устроены стратегии: сигналы, индикаторы и каскад сайзинга

### Семейство 1: Чистый Марков (`markov_pure` — [`markov_pure_signal_plugin.py`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CORE/STRATEGY/markov_pure_signal_plugin.py))
Модель прогнозирует смену рыночного режима по **дневным закрытиям (00:00 UTC)** без заглядывания в будущее:
1. **Доходность за 20 дней:** при закрытии каждых полных суток считается $\text{ret}_{20} = (C_d - C_{d-20}) / C_{d-20}$.
2. **Адаптивные 3-кластерные пороги (`gmm_3state` в [`_markov_thresholds.py`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CORE/STRATEGY/_markov_thresholds.py)):**
   * Каждые `7` дней на скользящем окне `180` последних дней EM-алгоритм Gaussian Mixture Model раскладывает историю доходностей монеты на 3 кластера (`BEAR`, `SIDEWAYS`, `BULL`), автоматически подстраивая границы под волатильность каждой монеты *(если данных мало — fallback на 30%-е квантили или `±5%`)*.
3. **Матрица переходов 3×3 (сглаживание Лапласа `alpha = 1.0`):**
   * По всей доступной истории переходов между дневными режимами строится матрица вероятностей $P$ (прогноз на `1` день вперёд = $P^1$).
   * Считается перевес вероятностей из текущего состояния $s$: $\text{net\_score} = P[s \to \text{BULL}] - P[s \to \text{BEAR}]$.
4. **Правило входа и реверса (`min_edge_ratio = 0.15`):**
   * Если $\text{net\_score} \ge +15\%$ → сигнал **`LONG`**.
   * Если $\text{net\_score} \le -15\%$ → сигнал **`SHORT`**.
   * Если позиция уже открыта в противоположную сторону — немедленный разворот (`OPPOSITE_SIGNAL`).
5. **Анти-боковиковые фильтры (включены в `antichop`, `atr_btc_sl5` и `shield`):**
   * **`block_in_sideways = true`:** запрещает открывать новые сделки, пока текущий кластер GMM — боковик `SIDEWAYS`, даже если у матрицы есть формальный перевес.
   * **Коэффициент эффективности Кауфмана [`RollingEfficiencyRatio`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CORE/STRATEGY/_indicators.py) (`ER >= 0.20`, окно `18` 4H свечей = `3` дня):**
     $$\text{ER} = \frac{|\text{Close}_t - \text{Close}_{t-18}|}{\sum_{i=t-17}^{t} |\text{Close}_i - \text{Close}_{i-1}|}$$
     В пиле (например, январь–март 2025) цена ходит на месте, шум в знаменателе большой, `ER < 0.20` — вход блокируется до направленного импульса.

### Семейство 2: `EMA 7/25` + Марков ([`ema_cross_signal_plugin.py`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CORE/STRATEGY/ema_cross_signal_plugin.py))
1. **Импульсный кроссовер (`EMACrossCalculator`):**
   * На 4H закрытиях считаются `EMA(7)` и `EMA(25)` (с SMA-инициализацией первых `N` баров) и разница $d_t = \text{EMA}_7 - \text{EMA}_{25}$.
   * **`CROSS_UP` (`LONG`):** 2 свечи назад $d_{t-2} < 0$, сейчас $d_t > 0$, спред рос ($d_{t-1} > d_{t-2}$) **и ускоряется**: $|d_t - d_{t-1}| > |d_{t-1} - d_{t-2}|$.
   * **`CROSS_DOWN` (`SHORT`):** зеркально сверху вниз с ускоряющимся расхождением. Переворот по встречному сигналу.
2. **Асимметричный Марковский гейт ([`MarkovRegimeFilter`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CORE/STRATEGY/markov_regime_filter.py), в миксах):**
   * Матрица 3×3 на фиксированных порогах `±5%` за 20 дней и скользящем окне переходов `180` дней.
   * **Жёстко блокирует `LONG` в `BEAR`** (`block_long_in_bear = true`), но **разрешает `SHORT` в `BULL`** (`block_short_in_bull = false`, чтобы не пропускать ранние развороты сверху).
3. **Индикатор силы тренда Уайлдера `ADX(14)` (`IncrementalADX`) и режимы входа:**
   * В **`adx20`:** вход разрешён только при `ADX(14) >= 20`.
   * В **`probe20` / `probe20_atr_sl5`:** если рынок в `SIDEWAYS` или `ADX < 20` (`21` в новой версии), открывается **проба `20%`**. Когда позиция выходит в прибыль, `SIDEWAYS` сломан и `ADX >= 21`, оркестратор автоматически **докупает до `100%`** (`submit_scale_in_market`). *(В Step 1 проверены пробы `20% / 40% / 60%`: `20%` победила всухую: `5Y +$12,286`, DD `27.41%`, Calmar `0.89` против `29.33% / 0.79` при `40%` и `31.36% / 0.69` при `60%`.)*
   * В **`hybrid_sl5`:** при `ADX < 15` (мёртвый рынок) вход **запрещён**, в зоне `15 <= ADX < 21` (или `SIDEWAYS`) идёт **проба `20%`**, при выходе в прибыль и пробое `ADX >= 21` — **добор до `100%`**.

### Как считается итоговый размер каждой позиции (каскад множителей)
В базовых паспортах на `6` монет объём на 1 монету фиксирован: **`$1,666.67`** (`$10,000 / 6`, `risk_per_trade_ratio = null`, `leverage = 2`). В **Боевом паспорте №3 (`top15_r15`)** используется динамический расчёт от риска до стопа `5 × ATR` (`risk_per_trade_ratio = 0.015`, `fixed_notional_usd = null`): база автоматически растёт со счётом и сжимается в просадке. При входе база последовательно умножается на активные множители:
$$\text{Notional} = \$1{,}666.67 \times M_{\text{signal}} \times M_{\text{atr}} \times M_{\text{markov\_filter}} \times M_{\text{sentinel}}$$

1. **`M_signal` (уверенность Маркова ИЛИ размер пробы):**
   * В `markov_pure`: $M_{\text{conf}} = \text{clamp}(|\text{net\_score}| \times P[s,s] \times 2.5,\; 0.50,\; 1.75)$ — от `$833` при слабом сигнале до `$2,917` при сильном и устойчивом тренде.
   * В `ema_cross`: `0.20` на пробе в боковике/слабом ADX, иначе `1.00`.
2. **`M_atr` (`ATR%` волатильностный сайзинг в `*_atr*`, `shield`, `hybrid_sl5`):**
   * Выравнивает дневной риск по монетам: $M_{\text{atr}} = \text{clamp}(0.02 / (\text{ATR}(14) / \text{Close}),\; 0.35,\; 1.25)$ *(для `EMA` верхняя граница `1.30`)*. Спокойная `ETH` получает больший объём, а резкая `DOGE`/`LTC` пропорционально сжимается; этот же множитель применяется при доборе `check_scale_in`.
3. **`M_markov_filter` (динамический сайзинг фильтра, только в `EMA+Марков`):**
   * Если режим совпадает со сделкой и диагональная устойчивость $P[s,s] \ge 0.70$ → **`1.25x`** (`+25%`), при обычном совпадении → `1.00x`, в `SIDEWAYS` → **`0.75x`**, против режима → **`0.50x`**.
4. **`M_sentinel` (кросс-рыночный фильтр [`MarketSentinel`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CORE/STRATEGY/market_sentinel.py) в `atr_btc_sl5`, `shield`, `hybrid_sl5`):**
   * **Сентимент `BTCUSDT`** (по 4H `EMA(50)`, `ADX(14)` и 20-дневной доходности без торговли BTC): в фазе `CHOP` (`BTC ADX < 18`) → **`×0.50`** всем сделкам; `LONG` в `BTC BEAR` → **`×0.40`** (или полная блокировка в `shield`); `SHORT` в `BTC BULL` → **`×0.50`**.
   * **`Market Breadth`** (в `shield`): считает долю трендовых монет из `6` (`ADX(14) >= 18` и Марков `!= SIDEWAYS`). Если их **меньше `3` из `6`**, весь портфель переходит в защитный режим **`×0.50`**.

---

## 3. Сводка за 5 лет на продакшн-движке (`2021-11-01 .. 2026-11-18`, `$10,000 / 2x / $1,666.67`)

Источник: [`prod_upgrades_10k_2x_5y.json`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBacktest/REPORTS/prod_upgrades_10k_2x_5y.json).

### 3.1. Главные итоги (семейство Маркова vs лидеры `EMA 7/25`)

| Показатель за 5 лет (`$10,000 / 2x`) | Марков **`atr_btc_sl5`** *(флагман)* | Марков **`shield`** *(броня)* | Марков `antichop` | Марков база `15%` | `EMA` **`probe20_atr_sl5`** | `EMA` **`hybrid_sl5`** | `EMA` `probe20` *(база)* | Чистые `EMA 7/25` |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Чистая прибыль ($)** | **+$46,215.43** | +$39,783.18 | +$41,111.09 | +$29,328.88 | **+$12,378.59** | +$11,952.85 | +$11,682.34 | +$10,654.13 |
| **Доходность к `$10,000`** | **+462.15%** (~$56,215) | +397.83% (~$49,783) | +411.11% | +293.29% | **+123.79%** | +119.53% | +116.82% | +106.54% |
| **Всего сделок (по стопу SL)** | 329 *(140 SL)* | 327 *(140 SL)* | **270** *(0 SL)* | 293 *(0 SL)* | 1,241 *(111 SL)* | 939 *(119 SL)* | 1,191 *(0 SL)* | 1,523 *(0 SL)* |
| **Доля прибыльных сделок** | 35.87% | 36.09% | **43.33%** | 42.66% | 24.50% | 28.97% | 25.78% | 32.90% |
| **Ср. прибыль / ср. убыток** | +$718.16 / **-$182.60** | +$627.18 / **-$163.75** | +$819.89 / -$358.27 | +$694.96 / -$342.50 | +$161.36 / -$39.14 | +$153.46 / -$44.66 | +$180.50 / -$49.47 | +$187.10 / -$81.29 |
| **Вся прибыль / все убытки** | **2.20** | **2.16** | 1.75 | 1.51 | 1.34 | **1.40** | 1.27 | 1.13 |
| **Макс. просадка (Мин. счёт)** | **39.86%** (`$7,439`) | **37.10%** (**`$7,894`**) | 76.59% (`$3,700`) | 80.77% (`$3,132`) | 34.78% (`$9,129`) | 32.68% (`$8,358`) | **28.23%** (**`$9,710`**) | 42.84% (`$7,953`) |
| **Доходность / просадка (Calmar)** | **2.29** | **2.12** | 1.06 | 0.72 | 0.70 | 0.72 | **0.82** | 0.49 |
| **Серии: побед / убытков** | 9 / 14 | 9 / 14 | **9 / 12** | 8 / 14 | 9 / 23 | 7 / 16 | 7 / 22 | 8 / 18 |
| **Long / Short ($)** | +$34,133 / +$12,082 | +$28,290 / +$11,493 | +$30,683 / +$10,428 | +$19,092 / +$10,237 | +$8,682 / +$3,696 | +$7,691 / +$4,262 | +$7,827 / +$3,855 | +$8,799 / +$1,856 |
| **Комиссии за 5 лет** | $648.28 | **$570.69** | $719.92 | $774.40 | $1,233.74 | $919.46 | $1,360.20 | $2,541.38 |

### 3.2. Разбивка по годам за 5 лет (`$10,000 / 2x`)

| Год | Марков **`atr_btc_sl5`** | Марков **`shield`** | Марков `antichop` | `EMA` **`probe20_atr_sl5`** | `EMA` **`hybrid_sl5`** | `EMA` `probe20` *(база)* | `EMA` `ADX>=20` | Чистые `EMA 7/25` |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **2021** (нояб–дек) | -$1,003.44 | -$702.20 | -$1,327.23 | +$42.13 | -$185.77 | +$133.17 | -$15.76 | -$155.43 |
| **2022** (медвежка) | +$2,408.32 | +$2,921.06 | +$158.81 | +$1,590.82 | +$722.61 | **+$3,405.91** | +$1,169.64 | +$998.31 |
| **2023** | **-$231.09** | -$382.26 | -$2,628.33 | **+$4,063.05** | +$3,535.21 | +$2,675.61 | +$2,300.39 | +$3,353.03 |
| **2024** (бычий тренд) | **+$32,267.15** | +$26,969.02 | +$30,640.83 | +$2,733.21 | +$3,689.96 | +$4,672.81 | +$2,692.11 | +$8,634.29 |
| **2025** *(боковики)* | **+$3,185.80** | +$2,419.99 | +$1,254.42 | **+$25.96** | -$432.86 | -$2,396.85 | -$1,057.70 | -$4,099.37 |
| **2026** (по 18 нояб.) | +$9,588.69 | +$8,557.57 | **+$13,012.60** | +$3,923.42 | +$4,623.70 | +$3,191.64 | +$4,735.58 | +$1,923.31 |

### 3.3. Статистика по монетам за 5 лет (все 6 монет, включая `LTCUSDT`)

#### Марков **`atr_btc_sl5`** (`329` сд., `+$46,215.43` — **6/6 монет в плюсе!**) и **`shield`** (`327` сд., `+$39,783.18` — **6/6 в плюсе!**)
| Монета | `atr_btc_sl5`: Сд. (+/−) | `atr_btc_sl5`: В плюс | `atr_btc_sl5`: Прибыль | Комис. | `shield`: Сд. (+/−) | `shield`: Прибыль | Комис. |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `DOGEUSDT` | 58 (24/34) | 41.38% | **+$13,990.70** | $103.49 | 58 (24/34) | **+$13,555.22** | $93.81 |
| `SOLUSDT` | 49 (19/30) | 38.78% | **+$12,607.15** | $84.21 | 48 (19/29) | **+$6,544.30** | $66.19 |
| `ADAUSDT` | 59 (20/39) | 33.90% | **+$8,420.27** | $104.53 | 58 (20/38) | **+$8,566.94** | $85.41 |
| `ETHUSDT` | 48 (19/29) | 39.58% | **+$7,633.98** | $121.25 | 48 (19/29) | **+$7,201.05** | $109.33 |
| `XRPUSDT` | 59 (19/40) | 32.20% | **+$2,613.64** | $121.51 | 59 (19/40) | **+$3,082.25** | $106.68 |
| `LTCUSDT` | 56 (17/39) | 30.36% | **+$949.69** | $113.30 | 56 (17/39) | **+$833.43** | $109.28 |

#### `EMA` **`probe20_atr_sl5`** (`1,241` сд., `+$12,378.59`) и **`hybrid_sl5`** (`939` сд., `+$11,952.85`)
| Монета | `probe20_atr_sl5`: Сд. (+/−) | В плюс | Прибыль | Комис. | `hybrid_sl5`: Сд. (+/−) | В плюс | Прибыль | Комис. |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `SOLUSDT` | 185 (52/133) | 28.11% | **+$5,381.85** | $172.77 | 144 (50/94) | 34.72% | **+$6,092.31** | $134.22 |
| `DOGEUSDT` | 212 (50/162) | 23.58% | **+$4,001.56** | $211.00 | 166 (46/120) | 27.71% | **+$3,744.88** | $165.87 |
| `ADAUSDT` | 197 (58/139) | 29.44% | **+$2,352.56** | $193.31 | 147 (53/94) | 36.05% | **+$2,441.46** | $142.64 |
| `XRPUSDT` | 223 (49/174) | 21.97% | **+$1,647.11** | $221.90 | 168 (46/122) | 27.38% | **+$1,832.53** | $157.94 |
| `ETHUSDT` | 207 (47/160) | 22.71% | **+$166.39** | $212.95 | 158 (38/120) | 24.05% | -$812.83 | $160.87 |
| `LTCUSDT` | 217 (48/169) | 22.12% | -$1,170.88 | $221.81 | 156 (39/117) | 25.00% | -$1,345.48 | $157.92 |

---

## 4. Отдельно: последние полгода (`2026-05-19 .. 2026-11-18`, `$10,000 / 2x / $1,666.67`)

| Показатель за 6 месяцев | Марков **`atr_btc_sl5`** | Марков **`shield`** | Марков `antichop` | `EMA` **`hybrid_sl5`** | `EMA` `ADX>=20` | `EMA` `probe20` *(база)* | `EMA` **`probe20_atr_sl5`** | Чистые `EMA 7/25` |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Прибыль и доходность** | **+$7,544.82 (+75.45%)** | +$5,378.66 (+53.79%) | **+$8,235.84 (+82.36%)** | **+$3,521.46 (+35.21%)** | +$3,252.23 (+32.52%) | +$3,037.21 (+30.37%) | +$2,925.09 (+29.25%) | +$1,261.65 (+12.62%) |
| **Сделок (в плюс %)** | 31 (61.29%) | 31 (58.06%) | **27 (70.37%)** | 89 (39.33%) | **42 (61.90%)** | 103 (35.92%) | 108 (32.41%) | 131 (38.93%) |
| **Прибыль / убыток** | **5.18** | 4.26 | **5.55** | **2.52** | **2.66** | 2.18 | 1.95 | 1.21 |
| **Макс. просадка** | 18.59% (`$2,893`) | 17.99% (`$2,425`) | **16.62%** (`$2,558`) | 12.65% (`$1,740`) | **10.26%** (`$1,131`) | 11.29% (`$1,494`) | 13.66% (`$1,834`) | 20.52% (`$2,822`) |
| **Комиссии** | $61.91 | **$49.94** | $59.05 | $124.61 | $67.42 | $136.88 | $150.90 | $217.49 |

* **Марков `atr_btc_sl5` за полгода (все 6 монет в плюсе):** `ETH +$1,879.68`, `SOL +$1,857.15`, `LTC +$1,212.88`, `ADA +$1,209.81`, `XRP +$840.73`, `DOGE +$544.57`.
* **Марков `shield` за полгода (все 6 монет в плюсе):** `ADA +$1,194.61`, `SOL +$1,181.23`, `LTC +$1,120.60`, `ETH +$939.47`, `XRP +$509.76`, `DOGE +$432.99`.
* **`EMA hybrid_sl5` за полгода (`+$3,521.46`, рекорд EMA):** `LTC +$1,236.89`, `DOGE +$825.26`, `ADA +$753.83`, `SOL +$640.41`, `XRP +$360.68`, `ETH -$295.62`.

---

## 5. Проверка чистой `EMA 7/25` на дневном таймфрейме (`1d`)

Да, проверялась отдельно — за 5 лет при `$1,000` дала `+$654.14` (`+65.41%`), просадка `64.30%`, 4 убыточных периода из 6 (`2021`, `2022`, `2023`, `2025`), в плюсе только 3 монеты из 6 (`SOL`, `XRP`, `ADA`). Поэтому `4h` для `EMA 7/25` объективно лучше `1d`.

---

## 6. Главный итог: что ставить в прод

1. **Лучший флагман для прода — [`markov_pure_4h_gmm_conf_antichop_atr_btc_sl5.json`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/strategies/markov_pure_4h_gmm_conf_antichop_atr_btc_sl5.json):**
   * Вырос до **`+$46,215.43 (+462.15%)`** за 5 лет, **уменьшил максимальную просадку почти вдвое** (`76.59% → 39.86%`), поднял **Calmar до `2.29`** и **прибыль/убыток до `2.20`**, поднял минимальное дно счёта с `$3,700` до **`$7,439`**, заработал **`+$3,185.80`** в тяжёлом боковике 2025, стоит на бирже со **страховочным SL `5 × ATR`** и **вывел в плюс все 6 монет, включая `LTCUSDT` (`+$949.69`)**.
2. **Если нужен ещё более плавный ход — [`markov_pure_4h_gmm_conf_antichop_shield.json`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/strategies/markov_pure_4h_gmm_conf_antichop_shield.json):** `+$39,783.18 (+397.83%)`, просадка **`37.10%`**, лучшее дно счёта среди Марковских версий (**`$7,894`**), также **6/6 монет в плюсе**.
3. **Если выбирать среди `EMA 7/25` — пара [`probe20_atr_sl5`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/strategies/ema_cross_7_25_4h_markov_probe20_atr_sl5.json) / [`hybrid_sl5`](file:///c:/Users/user/Desktop/My_Pro/HP_EliteBook_735_old/MY/CLAVA/ClavaTrading/ClavaBot/CFG/strategies/ema_cross_7_25_4h_markov_hybrid_sl5.json):**
   * `probe20_atr_sl5` дал максимум прибыли для `EMA` (**`+$12,378.59`**) и первым вывел `2025` год в плюс (**`+$25.96`**).
   * `hybrid_sl5` (с отсевом `ADX < 15` и сентиментом `BTCUSDT`) поднял п/у до лучшего среди `EMA` (**`1.40`**), сжал 2025 год в 6x (`-$433`) и показал лучший полугодовой результат среди всех `EMA` (**`+$3,521.46`**, п/у `2.52`).
