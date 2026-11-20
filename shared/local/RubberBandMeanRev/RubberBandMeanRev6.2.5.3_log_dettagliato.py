# ============================================================================
# LICENSE: GPL-3.0
# Strategy: RubberBandMeanRev625
# Freqtrade Ultimate - Mean Reversion LONG, DCA Robusto, LEVERAGE-AGNOSTIC,
#                      LOGGING DETTAGLIATO + PLOTTING FreqUI + TELEGRAM AVANZATO
#
# v6.2.5 (basata ESATTAMENTE sulla logica 62417)
# ----------------------------------------------------------------------------
# LOGICA IDENTICA ALLA 62417: struttura di custom_exit, populate_indicators,
#   e tutti i metodi sono copiati dalla 62417 originale senza modifiche.
# AGGIUNTE:
#   - Logging per-trade (_log_trade_close) + statistiche aggregate in bot_exit
#   - plot_config FreqUI + colonne plotting cosmetiche (exhaustion normalizzata)
#   - Rimozione log "Trailing stop hit" (info level)
#   - Silenziamento logging in HYPEROPT (tip #201)
#   - TELEGRAM AVANZATO (5 messaggi custom non duplicati rispetto a Freqtrade):
#       1. Regime BTC → entry consentite/bloccate (gate completo: momentum E SMA200)
#       2. Circuit breaker → entry bloccate per drawdown (daily/weekly)
#       3. Breakeven → protezione attiva sul trade (1× per trade)
#       4. Trailing → trailing attivo sul trade (1× per trade)
#       5. Escursioni → analisi dettagliata post-chiusura (max/min intra-candela
#          e alla chiusura della candela, in % da prima entry E da prezzo medio,
#          + indicatori entry/exit)
#
# CALIBRAZIONE: backtest 625 DEVE essere bit-per-bit identico a 62417.
# ============================================================================

# --- DO NOT CHANGE THIS LINES ---
from freqtrade.strategy import (
    IStrategy,
    DecimalParameter,
    CategoricalParameter,
    IntParameter,
)
from freqtrade.persistence import Trade
from freqtrade.enums import RunMode
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import talib.abstract as ta
import logging
import requests
# --- END OF FIXED AREA ---

logger = logging.getLogger(__name__)


def _ensure_utc(dt: datetime) -> datetime:
    """
    Garantisce che il datetime sia in UTC per confronti sicuri con Freqtrade.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class RubberBandMeanRev6253(IStrategy):
    """
    ╔══════════════════════════════════════════════════════════════════════════╗
    ║                  RUBBERBAND MEAN REVERSION STRATEGY                      ║
    ║                        (LEVERAGE-AGNOSTIC v6.2.4.17)                     ║
    ╚══════════════════════════════════════════════════════════════════════════╝

    PRINCIPIO LEVERAGE-AGNOSTIC
    ───────────────────────────
    Tutte le soglie di profitto/perdita sono espresse in PREZZO (distanza
    percentuale dal prezzo di entry), NON in percentuale del capitale
    leveraged. Questo rende la strategia indipendente dalla leva:

      price_profit = current_profit / leverage

    Con leva 2x: price_profit 0.0125 ⇔ current_profit 0.025 (comportamento 62411)
    Con leva 5x: price_profit 0.0125 ⇔ current_profit 0.0625 (stessa soglia prezzo)

    Cambiare leva = cambiare solo il sizing, non il fenomeno di mercato
    che la strategia cerca di catturare (tip #56, #62, #143).

    SISTEMA DI USCITA A SETTE LIVELLI (invariato da 62411, soglie in prezzo)
    ─────────────────────────────────────────────────────────────────────────
    1. PROFIT LOCK-IN (Breakeven + Trailing ATR on-exchange)
    2. ELASTIC MA TARGET
    3. MA5 CROSS (profit exit)
    4. ROCP BOUNCE (profit exit)
    5. TIME-LOSS DINAMICO (6-48h)
    6. HORIZONTAL BARRIER (24h, 5×ATR)
    7. TIME EXIT (max_hold_days)
    """

    # ========================================================================
    # SEZIONE 1: CONFIGURAZIONE BASE
    # ========================================================================

    INTERFACE_VERSION = 3

    can_short: bool = False
    timeframe = "15m"
    minimal_roi = {"0": 100}
    trailing_stop = False
    stoploss = -0.99
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    startup_candle_count = 250
    position_adjustment_enable = True

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "emergency_exit": "market",
        "force_entry": "market",
        "force_exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": True,
        "stoploss_on_exchange_interval": 30,
        "stoploss_on_exchange_limit_ratio": 0.99,
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    # ========================================================================
    # SEZIONE 2: COSTANTI DI STRATEGIA
    # ========================================================================

    MAX_TOTAL_EXPOSURE_PCT = 1.0
    CONFIDENCE_CAP = 1.5

    DAILY_LOSS_LIMIT_PCT = 0.065
    WEEKLY_LOSS_LIMIT_PCT = 0.08

    MAX_OPEN_TRADES = 4

    # ──────────────────────────────────────────────────────────────────────────
    # SOGLIE DI PROFITTO/PERDITA IN PREZZO (leverage-agnostic, v6.2.4.17)
    # ──────────────────────────────────────────────────────────────────────────
    BREAKEVEN_PRICE_THRESHOLD = 0.0125
    TRAILING_ACTIVATION_PRICE = 0.025
    ELASTIC_MA_MIN_PRICE_PROFIT = 0.005
    MA5_CROSS_MIN_PRICE_PROFIT = 0.004
    ROCP_BOUNCE_MIN_PRICE_PROFIT = 0.005
    TIME_LOSS_HALF_PRICE = -0.025
    TIME_LOSS_FULL_PRICE = -0.01
    CATASTROPHE_PRICE_THRESHOLD = -0.175

    HORIZONTAL_BARRIER_ATR_MULTIPLE = 5.0

    # ──────────────────────────────────────────────────────────────────────────
    # PARAMETRI DCA ROBUSTO
    # ──────────────────────────────────────────────────────────────────────────
    DCA_COOLDOWN_SECONDS = 900

    # ──────────────────────────────────────────────────────────────────────────
    # PARAMETRI TIME-LOSS DINAMICO
    # ──────────────────────────────────────────────────────────────────────────
    TIME_LOSS_BASE_HOURS = 12.0
    TIME_LOSS_MAX_HOURS = 48.0
    TIME_LOSS_MIN_HOURS = 6.0

    VOLATILITY_BONUS_MULT = 2.0
    ELASTIC_DISTANCE_MULT = 2.0
    RUBBERBAND_BONUS_MULT = 2.0
    BEAR_REGIME_PENALTY_MILD = -3.0
    BEAR_REGIME_PENALTY_STRONG = -6.0

    # ──────────────────────────────────────────────────────────────────────────
    # SOGLIE PER PLOTTING (costanti di classe, NON toccano la logica)
    # ──────────────────────────────────────────────────────────────────────────
    MIN_ATR_PCT_FOR_ENTRY = 0.005
    BTC_FILTER_MOM_THRESHOLD = -0.30
    ELASTIC_EXIT_Z_THRESHOLD = -0.3

    # ========================================================================
    # SEZIONE 3: PARAMETRI HYPEROPT
    # ========================================================================

    rocp_entry_long = DecimalParameter(-0.15, -0.04, default=-0.05, space="buy")
    rubberband_z_long = DecimalParameter(-3.5, -0.5, default=-1.2, space="buy")
    exhaustion_long = DecimalParameter(-0.05, 0.0, default=-0.01, space="buy")

    exit_rocp_bounce = DecimalParameter(0.01, 0.08, default=0.03, space="sell")
    exit_rubberband_z = DecimalParameter(-0.5, 0.8, default=0.0, space="sell")

    max_safety_orders = IntParameter(1, 5, default=4, space="buy")
    safety_order_volume_scale = DecimalParameter(1.2, 1.6, default=1.05, space="buy")
    safety_step_pct = DecimalParameter(0.015, 0.04, default=0.02, space="buy")
    max_hold_days = IntParameter(2, 21, default=10, space="sell")

    # ========================================================================
    # PLOT CONFIG per FreqUI / freqtrade plot-dataframe
    # ========================================================================
    plot_config = {
        "main_plot": {
            "elastic_ma": {"color": "#f4a261", "type": "line"},
            "ma_5":       {"color": "#e9c46a", "type": "line"},
            "sma_200":    {"color": "#2a9d8f", "type": "line"},
        },
        "subplots": {
            "Z-Score": {
                "rubberband_z":    {"color": "#e76f51", "type": "line"},
                "rb_z_entry_line": {"color": "#ff5555", "type": "line"},
                "rb_z_exit_line":  {"color": "#888888", "type": "line"},
            },
            "Exhaustion": {
                "exh_display":   {"color": "#4cc9f0", "type": "line"},
                "exh_zero_line": {"color": "#888888", "type": "line"},
            },
            "ROCP": {
                "rocp_5":          {"color": "#8ab17d", "type": "line"},
                "rocp_20":         {"color": "#b5838d", "type": "line"},
                "rocp_entry_line": {"color": "#ff5555", "type": "line"},
                "rocp_exit_line":  {"color": "#888888", "type": "line"},
            },
            "ATR %": {
                "atr_pct":      {"color": "#457b9d", "type": "line"},
                "atr_min_line": {"color": "#ff5555", "type": "line"},
            },
            "BTC mom": {
                "btc_mom_avg":          {"color": "#a0a0b8", "type": "line"},
                "btc_bear_mild_line":   {"color": "#888888", "type": "line"},
                "btc_bear_strong_line": {"color": "#ff5555", "type": "line"},
            },
        },
    }

    # ========================================================================
    # SEZIONE 4: INIT E LIFECYCLE
    # ========================================================================

    def bot_start(self, **kwargs) -> None:
        """
        Inizializzazione all'avvio del bot.
        """
        self._wallet_snapshot = {}
        self._trail_prices = {}
        self._trade_records = []
        
        # Stato per notifiche Telegram (solo cambio stato, no spam)
        self._last_entry_gate_open = None
        self._circuit_breaker_active = {"daily": False, "weekly": False}
        self._breakeven_activated = set()
        self._trailing_activated = set()

        self._dca_stats = {
            "dca_level_1": 0, "dca_level_2": 0, "dca_level_3": 0, "dca_level_4": 0,
            "trades_with_0_dca": 0, "trades_with_1_dca": 0, "trades_with_2_dca": 0,
            "trades_with_3_dca": 0, "trades_with_4_dca": 0,
            "total_dca_volume": 0.0, "total_dca_count": 0,
        }

        logger.info("=" * 70)
        logger.info("🚀 RubberBandMeanRev625 (LEVERAGE-AGNOSTIC + LOG + PLOT + TELEGRAM)")
        logger.info("=" * 70)
        logger.info(f"   MAX_TOTAL_EXPOSURE_PCT: {self.MAX_TOTAL_EXPOSURE_PCT}")
        logger.info(f"   MAX_OPEN_TRADES: {self.config.get('max_open_trades', self.MAX_OPEN_TRADES)}")
        logger.info(f"   max_safety_orders: {self.max_safety_orders.value}")
        logger.info(f"   safety_order_volume_scale: {self.safety_order_volume_scale.value}")
        logger.info(f"   safety_step_pct: {self.safety_step_pct.value}")
        logger.info(f"   max_hold_days: {self.max_hold_days.value}")
        logger.info(f"   horizontal_barrier_atr_multiple: {self.HORIZONTAL_BARRIER_ATR_MULTIPLE}")
        logger.info(f"   ✓ LEVERAGE-AGNOSTIC: soglie in PREZZO, non leveraged")
        logger.info(f"     Breakeven: {self.BREAKEVEN_PRICE_THRESHOLD:.2%} prezzo")
        logger.info(f"     Trailing: {self.TRAILING_ACTIVATION_PRICE:.2%} prezzo")
        logger.info(f"     Catastrophe: {self.CATASTROPHE_PRICE_THRESHOLD:.2%} prezzo")
        logger.info(f"   ⚠️ DCA cooldown: {self.DCA_COOLDOWN_SECONDS}s")
        logger.info(f"   ⚠️ Time-Loss Dinamico (6-48h)")
        logger.info(f"     Base: {self.TIME_LOSS_BASE_HOURS}h")
        logger.info(f"     Range: [{self.TIME_LOSS_MIN_HOURS}h, {self.TIME_LOSS_MAX_HOURS}h]")
        logger.info(f"   ✓ FIXED: Trailing con stop on-exchange e trigger live")
        logger.info(f"   ✓ LOGGING + PLOTTING + TELEGRAM AVANZATO attivi")
        logger.info("=" * 70)

    def bot_exit(self, **kwargs) -> None:
        """
        Stampa statistiche DCA aggregate alla fine del backtest/live.
        """
        if hasattr(self, '_trail_prices'):
            self._trail_prices.clear()

        run_mode = self.config.get("runmode", RunMode.OTHER)
        if run_mode == RunMode.HYPEROPT:
            return

        stats = self._dca_stats
        total_trades = sum(stats[f"trades_with_{i}_dca"] for i in range(5))

        logger.info("=" * 70)
        logger.info("📊 STATISTICHE DCA FINALI")
        logger.info("=" * 70)
        logger.info(f"  Trade totali chiusi: {total_trades}")
        logger.info(f"  DCA totali eseguiti: {stats['total_dca_count']}")
        logger.info(f"  Volume DCA totale: {stats['total_dca_volume']:.2f} USDT")

        if total_trades > 0:
            logger.info(f"  Media DCA per trade: {stats['total_dca_count']/total_trades:.2f}")

        logger.info("-" * 70)

        logger.info("  DISTRIBUZIONE DCA PER LIVELLO:")
        for level in range(1, 5):
            key = f"dca_level_{level}"
            count = stats.get(key, 0)
            total_dca = max(stats['total_dca_count'], 1)
            pct = (count / total_dca) * 100
            bar = "█" * int(pct / 2)
            logger.info(f"    {key}: {count:>4} hit ({pct:>5.1f}%) {bar}")

        logger.info("-" * 70)

        logger.info("  DISTRIBUZIONE TRADE PER NUMERO DCA:")
        for n_dca in range(5):
            key = f"trades_with_{n_dca}_dca"
            count = stats.get(key, 0)
            pct = (count / max(total_trades, 1)) * 100
            bar = "█" * int(pct / 2)
            nome = "Solo entry" if n_dca == 0 else f"{n_dca} DCA"
            logger.info(f"    {nome:>12}: {count:>4} trade ({pct:>5.1f}%) {bar}")

        # ── STATISTICHE AGGREGATE PER-TRADE ──────────────────────────────
        if hasattr(self, "_trade_records") and self._trade_records:
            recs = self._trade_records

            logger.info("-" * 70)
            logger.info("  STATISTICHE PER EXIT REASON:")
            for reason in sorted(set(r["exit_reason"] for r in recs)):
                sub = [r for r in recs if r["exit_reason"] == reason]
                n = len(sub)
                logger.info(
                    f"    {reason:>24}: n={n:>4} | "
                    f"profit={sum(r['profit'] for r in sub) / n:+7.2%} | "
                    f"hold={sum(r['hold_hours'] for r in sub) / n:5.1f}h | "
                    f"dca={sum(r['n_dca'] for r in sub) / n:.2f} | "
                    f"max={sum(r['max_exc'] for r in sub) / n:+6.2%} "
                    f"min={sum(r['min_exc'] for r in sub) / n:+6.2%}"
                )

            logger.info("-" * 70)
            logger.info("  STATISTICHE PER NUMERO DCA:")
            for n_dca in range(5):
                sub = [r for r in recs if r["n_dca"] == n_dca]
                if not sub:
                    continue
                n = len(sub)
                win = sum(1 for r in sub if r["profit"] > 0)
                logger.info(
                    f"    {n_dca} DCA: n={n:>4} | "
                    f"profit={sum(r['profit'] for r in sub) / n:+7.2%} | "
                    f"winrate={win / n:6.1%}"
                )

            logger.info("-" * 70)
            logger.info("  ESCURSIONI PREZZO (media su tutti i trade):")
            logger.info(f"    Max excursion medio: "
                        f"{sum(r['max_exc'] for r in recs) / len(recs):+.2%} sopra entry")
            logger.info(f"    Min excursion medio: "
                        f"{sum(r['min_exc'] for r in recs) / len(recs):+.2%} sotto entry")

        logger.info("=" * 70)

    # ========================================================================
    # SEZIONE 5: HELPER METHODS
    # ========================================================================

    def _get_stake_currency(self) -> str:
        return self.config.get("stake_currency", "USDT")

    def _is_futures_mode(self) -> bool:
        trading_mode = self.config.get("trading_mode", "spot")
        if trading_mode == "futures":
            return True
        ccxt_config = self.config.get("exchange", {}).get("ccxt_config", {})
        options = ccxt_config.get("options", {})
        default_type = options.get("defaultType", "spot")
        return default_type in ("swap", "future", "futures")

    def _get_btc_pair(self) -> str:
        stake = self._get_stake_currency()
        if self._is_futures_mode():
            return f"BTC/{stake}:{stake}"
        return f"BTC/{stake}"

    def _get_tradable_wallet(self) -> float:
        wallet = self.wallets.get_total(self.config["stake_currency"])
        tradable_balance_ratio = self.config.get("tradable_balance_ratio", 0.99)
        return wallet * tradable_balance_ratio

    def _get_first_entry_price(self, trade) -> float:
        try:
            first_entry_order = next(
                (o for o in trade.orders
                 if o.ft_order_side == "buy" and o.ft_order_tag == "long_mean_rev"),
                None
            )
            if first_entry_order and first_entry_order.price:
                return float(first_entry_order.price)
        except Exception:
            pass
        return float(trade.open_rate)

    def _send_telegram(self, message: str) -> None:
        """
        Invia messaggio Telegram se configurato e non in hyperopt/backtest.
        No-op silenzioso su errori (le notifiche non devono mai bloccare il trading).
        """
        run_mode = self.config.get("runmode", RunMode.OTHER)
        if run_mode in (RunMode.HYPEROPT, RunMode.BACKTEST):
            return
        
        telegram_config = self.config.get("telegram", {})
        if not telegram_config.get("enabled"):
            return
        
        token = telegram_config.get("token")
        chat_id = telegram_config.get("chat_id")
        
        if not token or not chat_id:
            return
        
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": message[:4090],  # Telegram limit 4096 chars
                "parse_mode": "HTML"
            }
            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            logger.warning(f"Errore invio Telegram: {e}")

    # ========================================================================
    # SEZIONE 6: INFORMATIVE PAIRS
    # ========================================================================

    def informative_pairs(self):
        btc_pair = self._get_btc_pair()
        return [(btc_pair, self.timeframe)]

    # ========================================================================
    # SEZIONE 7: POPULATE INDICATORS
    # ========================================================================

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """
        Calcolo di tutti gli indicatori tecnici necessari.
        """
        dataframe["rocp_5"] = ta.ROCP(dataframe, timeperiod=5)
        dataframe["rocp_20"] = ta.ROCP(dataframe, timeperiod=20)

        vw_close = dataframe["close"] * dataframe["volume"]
        ema_vw_close = pd.Series(ta.EMA(vw_close, timeperiod=20), index=dataframe.index)
        ema_volume = pd.Series(ta.EMA(dataframe["volume"], timeperiod=20), index=dataframe.index)
        dataframe["elastic_ma"] = ema_vw_close / ema_volume.replace(0, np.nan)

        dataframe["atr_14"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["stretch"] = (
            (dataframe["close"] - dataframe["elastic_ma"]) / dataframe["atr_14"]
        )

        stretch_mean = dataframe["stretch"].rolling(window=100).mean()
        stretch_std = dataframe["stretch"].rolling(window=100).std()
        dataframe["rubberband_z"] = (
            (dataframe["stretch"] - stretch_mean) / stretch_std.replace(0, np.nan)
        )

        price_range = (dataframe["high"] - dataframe["low"]).replace(0, np.nan)
        direction = np.sign(dataframe["close"] - dataframe["open"])
        dataframe["volume_pressure"] = (dataframe["volume"] * direction) / price_range

        sma_pressure = pd.Series(ta.SMA(dataframe["volume_pressure"], timeperiod=10), index=dataframe.index)
        sma_volume = pd.Series(ta.SMA(dataframe["volume"], timeperiod=10), index=dataframe.index)
        dataframe["exhaustion"] = sma_pressure / sma_volume.replace(0, np.nan)

        dataframe["sma_200"] = ta.SMA(dataframe, timeperiod=200, field="close")
        dataframe["ma_5"] = ta.SMA(dataframe, timeperiod=5, field="close")
        dataframe["volume_sma_20"] = pd.Series(ta.SMA(dataframe["volume"], timeperiod=20), index=dataframe.index)

        btc_pair = self._get_btc_pair()
        informative_btc = self.dp.get_pair_dataframe(btc_pair, self.timeframe)

        if not informative_btc.empty and len(informative_btc) >= 250:
            btc_slim = informative_btc[["date", "close"]].copy()
            btc_slim = btc_slim.rename(columns={"close": "close_btc"})

            dataframe = dataframe.sort_values("date").reset_index(drop=True)
            btc_slim = btc_slim.sort_values("date").reset_index(drop=True)

            dataframe = pd.merge_asof(dataframe, btc_slim, on="date", direction="backward")

            close_btc_shift_25 = dataframe["close_btc"].shift(25).replace(0, np.nan)
            close_btc_shift_100 = dataframe["close_btc"].shift(100).replace(0, np.nan)

            dataframe["btc_mom_25"] = (
                (dataframe["close_btc"] - dataframe["close_btc"].shift(25)) / close_btc_shift_25
            )
            dataframe["btc_mom_100"] = (
                (dataframe["close_btc"] - dataframe["close_btc"].shift(100)) / close_btc_shift_100
            )

            dataframe["btc_mom_avg"] = (
                dataframe["btc_mom_25"].ffill().fillna(0) +
                dataframe["btc_mom_100"].ffill().fillna(0)
            ) / 2

            dataframe["sma_btc_200"] = pd.Series(ta.SMA(dataframe["close_btc"], timeperiod=200), index=dataframe.index)
            dataframe["has_btc_data"] = dataframe["close_btc"].notna()
        else:
            dataframe["btc_mom_avg"] = 0.0
            dataframe["sma_btc_200"] = np.nan
            dataframe["close_btc"] = np.nan
            dataframe["has_btc_data"] = False

        # ── Colonne DISPLAY per plotting (cosmetiche, non toccano la logica) ──
        dataframe["rb_z_entry_line"] = self.rubberband_z_long.value
        dataframe["rb_z_exit_line"] = self.ELASTIC_EXIT_Z_THRESHOLD
        dataframe["exh_display"] = dataframe["exhaustion"] * dataframe["atr_14"]
        dataframe["exh_zero_line"] = 0.0
        dataframe["atr_pct"] = dataframe["atr_14"] / dataframe["close"]
        dataframe["atr_min_line"] = self.MIN_ATR_PCT_FOR_ENTRY
        dataframe["btc_mom_filter_line"] = self.BTC_FILTER_MOM_THRESHOLD

        return dataframe

    # ========================================================================
    # SEZIONE 8: POPULATE ENTRY TREND + NOTIFICA REGIME BTC
    # ========================================================================

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """
        Condizioni di entry long (invariato da 62411).
        Notifica Telegram su cambio stato gate entry (momentum E SMA200).
        """
        # Monitoraggio gate entry BTC per notifiche (solo cambio di stato)
        if not dataframe.empty and "btc_mom_avg" in dataframe.columns:
            mom = dataframe["btc_mom_avg"].iloc[-1]
            close_btc = dataframe["close_btc"].iloc[-1] if pd.notna(dataframe["close_btc"].iloc[-1]) else 0.0
            sma_btc = dataframe["sma_btc_200"].iloc[-1] if pd.notna(dataframe["sma_btc_200"].iloc[-1]) else 0.0
            above_sma = close_btc > sma_btc
            mom_ok = mom > self.BTC_FILTER_MOM_THRESHOLD
            
            # Entry consentite solo se ENTRAMBE le condizioni vere
            entries_open = mom_ok and above_sma
            last_state = getattr(self, "_last_entry_gate_open", None)
            
            if last_state is not None and last_state != entries_open:
                if entries_open:
                    msg = (
                        f"🟢 <b>ENTRY RIABILITATE</b>\n\n"
                        f"<b>Momentum BTC:</b> {mom:+.2%} (soglia {self.BTC_FILTER_MOM_THRESHOLD:.0%})\n"
                        f"<b>BTC vs SMA200:</b> {'sopra ✅' if above_sma else 'sotto ❌'}\n"
                        f"\n⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                    )
                else:
                    motivo = []
                    if not mom_ok:
                        motivo.append(f"momentum {mom:+.2%} ≤ {self.BTC_FILTER_MOM_THRESHOLD:.0%}")
                    if not above_sma:
                        motivo.append("BTC sotto SMA200")
                    msg = (
                        f"⛔ <b>ENTRY BLOCCATE (BTC)</b>\n\n"
                        f"<b>Motivo:</b> {', '.join(motivo)}\n"
                        f"<b>Momentum BTC:</b> {mom:+.2%}\n"
                        f"<b>BTC vs SMA200:</b> {'sopra ✅' if above_sma else 'sotto ❌'}\n"
                        f"\n⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                    )
                self._send_telegram(msg)
            
            self._last_entry_gate_open = entries_open

        long_conditions = (
            (dataframe["rocp_20"] < self.rocp_entry_long.value) &
            (dataframe["rubberband_z"] < self.rubberband_z_long.value) &
            (dataframe["exhaustion"] < self.exhaustion_long.value) &
            (dataframe["close"] > dataframe["sma_200"]) &
            (dataframe["atr_14"] > dataframe["close"] * 0.005) &
            dataframe["rocp_20"].notna() &
            dataframe["rubberband_z"].notna() &
            dataframe["exhaustion"].notna() &
            dataframe["sma_200"].notna()
        )

        btc_filter = (
            (dataframe["btc_mom_avg"] > -0.30) &
            (dataframe["close_btc"] > dataframe["sma_btc_200"]) &
            dataframe["sma_btc_200"].notna() &
            dataframe["close_btc"].notna()
        )

        long_conditions = long_conditions & (~dataframe["has_btc_data"] | btc_filter)

        dataframe.loc[long_conditions, ["enter_long", "enter_tag"]] = (1, "long_mean_rev")

        return dataframe

    # ========================================================================
    # SEZIONE 9: POPULATE EXIT TREND
    # ========================================================================

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe.loc[:, ["exit_long", "exit_tag"]] = (0, "")
        return dataframe

    # ========================================================================
    # SEZIONE 10: CUSTOM STAKE AMOUNT
    # ========================================================================

    def custom_stake_amount(self, pair, current_time, current_rate, proposed_stake,
                            min_stake, max_stake, leverage, entry_tag, side, **kwargs):
        """
        Calcolo dello stake iniziale con logica dinamica basata su confidence.
        (Invariato da 62411.)
        """
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or dataframe.empty:
            return proposed_stake

        curr_time = _ensure_utc(current_time)
        past_df = dataframe[pd.to_datetime(dataframe["date"], utc=True) < curr_time]

        if past_df.empty:
            return proposed_stake

        last_candle = past_df.iloc[-1]

        rubberband_z_raw = last_candle.get("rubberband_z", 0) or 0
        confidence = np.clip(-rubberband_z_raw / 2.5, 1.0, self.CONFIDENCE_CAP)

        btc_mom = last_candle.get("btc_mom_avg", 0.0) or 0.0
        regime_scale = np.clip(0.4 + 0.6 * (btc_mom + 0.15) / 0.35, 0.4, 1.0)

        if btc_mom < -0.15:
            regime_scale *= 0.5

        max_dca = self.max_safety_orders.value
        scale = self.safety_order_volume_scale.value
        total_multiplier = (
            (max_dca + 1) if scale == 1.0
            else (scale**(max_dca + 1) - 1) / (scale - 1)
        )

        wallet_tradable = self._get_tradable_wallet()
        max_total_exposure = wallet_tradable * self.MAX_TOTAL_EXPOSURE_PCT

        effective_total_exposure = max_total_exposure * confidence * regime_scale

        max_effective_exposure = max_total_exposure * self.CONFIDENCE_CAP
        effective_total_exposure = min(effective_total_exposure, max_effective_exposure)

        base_stake = effective_total_exposure / total_multiplier
        final_stake = float(np.clip(base_stake, min_stake, max_stake))

        return final_stake

    # ========================================================================
    # SEZIONE 11: ADJUST TRADE POSITION (DCA) - SISTEMA ROBUSTO
    # ========================================================================

    def adjust_trade_position(self, trade, current_time, current_rate, current_profit,
                              min_stake, max_stake, current_entry_rate, current_exit_rate,
                              current_entry_profit, current_exit_profit, **kwargs):
        """
        Logica DCA con Martingala controllata e 5 fix robustezza.
        (Invariato da 62411.)
        """
        if self._is_daily_circuit_breaker_active(current_time):
            return None, None

        if self._is_weekly_circuit_breaker_active(current_time):
            return None, None

        if any(o.ft_order_side == "buy" and o.ft_is_open for o in trade.orders):
            return None, None

        filled_entries = [
            o for o in trade.orders
            if o.ft_order_side == "buy"
            and (o.status == "closed"
                 or (o.status == "canceled" and (o.filled or 0) > 0))
        ]
        n_dca_executed = len(filled_entries) - 1

        if n_dca_executed < 0:
            return None, None

        curr_time = _ensure_utc(current_time)
        if (curr_time - trade.open_date_utc).total_seconds() < self.DCA_COOLDOWN_SECONDS:
            return None, None

        if n_dca_executed >= self.max_safety_orders.value:
            return None, None

        first_entry_price = self._get_first_entry_price(trade)

        step = self.safety_step_pct.value * (n_dca_executed + 1)
        target_price = first_entry_price * (1 - step)

        if current_rate > target_price:
            return None, None

        if current_profit > 0.01:
            return None, None

        max_dca = self.max_safety_orders.value
        scale = self.safety_order_volume_scale.value
        total_multiplier = (
            (max_dca + 1) if scale == 1.0
            else (scale**(max_dca + 1) - 1) / (scale - 1)
        )

        base_stake = None
        max_total_exposure = None

        initial_entry_order = next(
            (o for o in trade.orders
             if o.ft_order_side == "buy" and o.ft_order_tag == "long_mean_rev"),
            None
        )

        if initial_entry_order:
            base_stake = float(initial_entry_order.stake_amount)
            max_total_exposure = base_stake * total_multiplier

        if base_stake is None:
            wallet_tradable = self._get_tradable_wallet()
            max_total_exposure = wallet_tradable * self.MAX_TOTAL_EXPOSURE_PCT
            base_stake = max_total_exposure / total_multiplier

        dca_stake = base_stake * (scale ** (n_dca_executed + 1))

        current_exposure = float(trade.stake_amount)

        if current_exposure + dca_stake > max_total_exposure:
            dca_stake = max_total_exposure - current_exposure
            if dca_stake < min_stake:
                return None, None

        dca_stake_final = float(np.clip(dca_stake, min_stake, max_stake))
        dca_level = n_dca_executed + 1
        tag = f"dca_level_{dca_level}"

        if tag in self._dca_stats:
            self._dca_stats[tag] += 1
        self._dca_stats["total_dca_count"] += 1
        self._dca_stats["total_dca_volume"] += dca_stake_final

        return dca_stake_final, tag

    # ========================================================================
    # SEZIONE 12: CUSTOM EXIT - LEVERAGE-AGNOSTIC (v6.2.4.17) + LOGGING + TELEGRAM
    # ========================================================================

    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
        """
        Logica di uscita a sette livelli con soglie LEVERAGE-AGNOSTIC.
        Notifica Telegram per breakeven e trailing (1× per trade).
        """
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or dataframe.empty:
            return None

        curr_time = _ensure_utc(current_time)
        past_df = dataframe[pd.to_datetime(dataframe["date"], utc=True) < curr_time]

        if past_df.empty or len(past_df) < 2:
            return None

        last_candle = past_df.iloc[-1]

        hold_time = current_time - trade.open_date_utc
        hold_hours = hold_time.total_seconds() / 3600
        hold_days = hold_time.total_seconds() / 86400.0

        filled_entries = [o for o in trade.orders if o.ft_order_side == "buy" and o.status == "closed"]
        n_dca_at_exit = len(filled_entries) - 1
        n_dca_at_exit_limited = min(n_dca_at_exit, 4)
        key = f"trades_with_{n_dca_at_exit_limited}_dca"
        if key in self._dca_stats:
            self._dca_stats[key] += 1

        def cleanup_trail():
            if hasattr(self, '_trail_prices') and trade.id in self._trail_prices:
                del self._trail_prices[trade.id]

        def close_with(reason):
            """Centralizza: log dettagliato + cleanup trail + return."""
            self._log_trade_close(
                trade, pair, dataframe, reason, current_rate, current_profit,
                curr_time, self._get_first_entry_price(trade), trade.leverage or 1.0,
            )
            cleanup_trail()
            return reason

        # ────────────────────────────────────────────────────────────────
        # CONVERSIONE LEVERAGE-AGNOSTIC (v6.2.4.17)
        # ────────────────────────────────────────────────────────────────
        leverage = trade.leverage or 1.0
        price_profit = current_profit / leverage

        if not hasattr(self, '_trail_prices'):
            self._trail_prices = {}

        first_entry_price = self._get_first_entry_price(trade)

        # ────────────────────────────────────────────────────────────────
        # EXIT 1: PROFIT LOCK-IN (Breakeven + Trailing ATR) + TELEGRAM
        # ────────────────────────────────────────────────────────────────

        # 1a. BREAKEVEN LOCK: prezzo +1.25% sopra entry → pavimento entry×1.003
        if price_profit > self.BREAKEVEN_PRICE_THRESHOLD:
            if trade.id not in self._breakeven_activated:
                msg = (
                    f"🛡️ <b>BREAKEVEN ATTIVATO</b>\n\n"
                    f"<b>Pair:</b> {pair}\n"
                    f"<b>Prezzo entry:</b> {first_entry_price:.6f}\n"
                    f"<b>Prezzo attuale:</b> {current_rate:.6f}\n"
                    f"<b>Profit prezzo:</b> {price_profit:+.2%}\n"
                    f"<b>Floor:</b> {first_entry_price * 1.003:.6f} (+0.3%)\n"
                    f"\n⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                )
                self._send_telegram(msg)
                self._breakeven_activated.add(trade.id)
            
            floor_price = first_entry_price * 1.003
            old_trail = self._trail_prices.get(trade.id, 0)
            new_trail = max(old_trail, floor_price)
            self._trail_prices[trade.id] = new_trail

        # 1b. TRAILING ATR: prezzo +2.5% sopra entry → trailing stop a 2.5×ATR
        if price_profit > self.TRAILING_ACTIVATION_PRICE:
            if trade.id not in self._trailing_activated:
                msg = (
                    f"🎯 <b>TRAILING ATR ATTIVATO</b>\n\n"
                    f"<b>Pair:</b> {pair}\n"
                    f"<b>Prezzo entry:</b> {first_entry_price:.6f}\n"
                    f"<b>Prezzo attuale:</b> {current_rate:.6f}\n"
                    f"<b>Profit prezzo:</b> {price_profit:+.2%}\n"
                    f"<b>ATR:</b> {last_candle['atr_14']:.6f}\n"
                    f"<b>Trail distance:</b> 2.5×ATR\n"
                    f"\n⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                )
                self._send_telegram(msg)
                self._trailing_activated.add(trade.id)
            
            if pd.notna(last_candle.get("atr_14")) and last_candle["atr_14"] > 0:
                atr_trail = last_candle["atr_14"] * 2.5
                trail_price = current_rate - atr_trail
                floor_price = first_entry_price * 1.003

                if trail_price > floor_price:
                    old_trail = self._trail_prices.get(trade.id, 0)
                    new_trail = max(old_trail, trail_price)
                    self._trail_prices[trade.id] = new_trail

                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            f"Trail updated for {pair} (trade {trade.id}): "
                            f"price={current_rate:.6f}, trail={new_trail:.6f}, "
                            f"price_profit={price_profit:.2%}"
                        )

                    if current_rate < new_trail:
                        return close_with("trailing_atr_exit")

        # 1c. TRAILING HIT: prezzo live tocca il trail → exit
        # NOTA: log "Trailing stop hit" RIMOSSO su richiesta
        if trade.id in self._trail_prices:
            trail_level = self._trail_prices[trade.id]
            if current_rate < trail_level:
                return close_with("trailing_atr_exit")

        # ────────────────────────────────────────────────────────────────
        # EXIT 2: ELASTIC MA TARGET (profitto minimo in prezzo)
        # ────────────────────────────────────────────────────────────────
        if (pd.notna(last_candle.get("elastic_ma")) and
            pd.notna(last_candle.get("rubberband_z")) and
            last_candle["close"] >= last_candle["elastic_ma"] and
            last_candle["rubberband_z"] > self.ELASTIC_EXIT_Z_THRESHOLD and
            price_profit > self.ELASTIC_MA_MIN_PRICE_PROFIT):
            return close_with("elastic_ma_target")

        # ────────────────────────────────────────────────────────────────
        # EXIT 3: MA5 CROSS (profitto minimo in prezzo)
        # ────────────────────────────────────────────────────────────────
        if (pd.notna(last_candle.get("ma_5")) and
            pd.notna(last_candle.get("rubberband_z")) and
            last_candle["close"] > last_candle["ma_5"] and
            last_candle["rubberband_z"] > self.exit_rubberband_z.value and
            price_profit > self.MA5_CROSS_MIN_PRICE_PROFIT):
            return close_with("ma5_cross_long")

        # ────────────────────────────────────────────────────────────────
        # EXIT 4: ROCP BOUNCE (profitto minimo in prezzo)
        # ───────────────────────────────────────────────────────────────
        if (pd.notna(last_candle.get("rocp_5")) and
            last_candle["rocp_5"] > self.exit_rocp_bounce.value and
            price_profit > self.ROCP_BOUNCE_MIN_PRICE_PROFIT):
            return close_with("rocp_bounce_long")

        # ────────────────────────────────────────────────────────────────
        # EXIT 5: TIME-LOSS DINAMICO (soglie in prezzo)
        # ────────────────────────────────────────────────────────────────

        dynamic_max_hours = self.TIME_LOSS_BASE_HOURS

        volatility_bonus = 0.0
        if pd.notna(last_candle.get("atr_14")) and last_candle["close"] > 0:
            atr_pct = last_candle["atr_14"] / last_candle["close"]
            volatility_bonus = max(0, (atr_pct - 0.01) * 100) * self.VOLATILITY_BONUS_MULT
        dynamic_max_hours += volatility_bonus

        elastic_bonus = 0.0
        if (pd.notna(last_candle.get("elastic_ma")) and
            pd.notna(last_candle.get("atr_14")) and
            last_candle["atr_14"] > 0):
            distance_atr = (last_candle["elastic_ma"] - last_candle["close"]) / last_candle["atr_14"]
            elastic_bonus = max(0, distance_atr) * self.ELASTIC_DISTANCE_MULT
        dynamic_max_hours += elastic_bonus

        rb_bonus = 0.0
        if pd.notna(last_candle.get("rubberband_z")):
            rb_bonus = max(0, -last_candle["rubberband_z"] - 1.5) * self.RUBBERBAND_BONUS_MULT
        dynamic_max_hours += rb_bonus

        regime_penalty = 0.0
        if pd.notna(last_candle.get("btc_mom_avg")):
            if last_candle["btc_mom_avg"] < -0.10:
                regime_penalty = self.BEAR_REGIME_PENALTY_STRONG
            elif last_candle["btc_mom_avg"] < -0.05:
                regime_penalty = self.BEAR_REGIME_PENALTY_MILD
        dynamic_max_hours += regime_penalty

        dynamic_max_hours = np.clip(
            dynamic_max_hours,
            self.TIME_LOSS_MIN_HOURS,
            self.TIME_LOSS_MAX_HOURS
        )

        # 5a. Taglia a metà tempo se prezzo -2.5% sotto entry
        if hold_hours > (dynamic_max_hours * 0.5) and price_profit < self.TIME_LOSS_HALF_PRICE:
            return close_with(f"time_loss_exit_{dynamic_max_hours:.0f}h")

        # 5b. Taglia a tempo pieno se prezzo -1% sotto entry
        if hold_hours > dynamic_max_hours and price_profit < self.TIME_LOSS_FULL_PRICE:
            return close_with(f"time_loss_exit_{dynamic_max_hours:.0f}h")

        # ────────────────────────────────────────────────────────────────
        # EXIT 6: HORIZONTAL BARRIER (5×ATR dopo 24h, già in prezzo)
        # ────────────────────────────────────────────────────────────────
        if hold_hours > 24 and pd.notna(last_candle.get("atr_14")) and last_candle["atr_14"] > 0:
            horizontal_barrier = first_entry_price - (
                self.HORIZONTAL_BARRIER_ATR_MULTIPLE * last_candle["atr_14"]
            )
            if last_candle["close"] < horizontal_barrier:
                return close_with("horizontal_barrier")

        # ────────────────────────────────────────────────────────────────
        # EXIT 7: TIME EXIT (chiusura forzata)
        # ────────────────────────────────────────────────────────────────
        if hold_days > self.max_hold_days.value:
            return close_with("time_exit")

        return None

    # ========================================================================
    # SEZIONE 12b: LOGGING PER-TRADE + TELEGRAM ESCURSIONI
    # ========================================================================

    def _log_trade_close(self, trade, pair, dataframe, exit_reason, current_rate,
                         current_profit, curr_time, first_entry_price, leverage):
        """
        Log dettagliato per-trade al momento della chiusura + accumulo del
        record in self._trade_records per le statistiche aggregate in bot_exit.
        Silenziato in HYPEROPT (tip #201).
        
        Telegram: invia SOLO escursioni max/min (informazione non duplicata
        rispetto al messaggio exit di default Freqtrade).
        Escursioni calcolate rispetto a:
        - Prima entry (logica leverage-agnostic della strategia)
        - Prezzo medio ponderato (include DCA, costo medio reale)
        """
        run_mode = self.config.get("runmode", RunMode.OTHER)
        if run_mode == RunMode.HYPEROPT:
            return

        entry_candle = exit_candle = None
        max_price = min_price = current_rate
        max_candle_close = min_candle_close = current_rate
        max_candle_time = min_candle_time = curr_time
        
        if dataframe is not None and not dataframe.empty:
            entry_df = dataframe[dataframe["date"] <= trade.open_date_utc]
            if not entry_df.empty:
                entry_candle = entry_df.iloc[-1]

            exit_df = dataframe[dataframe["date"] <= curr_time]
            if not exit_df.empty:
                exit_candle = exit_df.iloc[-1]

            trade_df = dataframe[
                (dataframe["date"] >= trade.open_date_utc) &
                (dataframe["date"] <= curr_time)
            ]
            if not trade_df.empty:
                max_price = float(trade_df["high"].max())
                min_price = float(trade_df["low"].min())
                
                # Trova max/min alla chiusura della candela
                max_idx = trade_df["high"].idxmax()
                min_idx = trade_df["low"].idxmin()
                max_candle_close = float(trade_df.loc[max_idx, "close"])
                min_candle_close = float(trade_df.loc[min_idx, "close"])
                max_candle_time = trade_df.loc[max_idx, "date"]
                min_candle_time = trade_df.loc[min_idx, "date"]

        dca_orders = [
            o for o in trade.orders
            if o.ft_order_side == "buy"
            and o.ft_order_tag != "long_mean_rev"
            and o.status == "closed"
        ]
        n_dca = len(dca_orders)
        dca_prices = ", ".join(f"{o.price:.6f}" for o in dca_orders) or "-"

        hold_hours = (curr_time - trade.open_date_utc).total_seconds() / 3600
        price_profit = current_profit / leverage
        
        # Escursioni rispetto alla PRIMA ENTRY (logica leverage-agnostic)
        max_exc = (max_price / first_entry_price - 1) if first_entry_price else 0.0
        min_exc = (min_price / first_entry_price - 1) if first_entry_price else 0.0
        
        # Escursioni rispetto al PREZZO MEDIO PONDERATO (include DCA)
        avg_entry_price = float(trade.open_rate)  # Freqtrade aggiorna con i DCA
        max_exc_avg = (max_price / avg_entry_price - 1) if avg_entry_price else 0.0
        min_exc_avg = (min_price / avg_entry_price - 1) if avg_entry_price else 0.0
        
        max_close_exc = (max_candle_close / first_entry_price - 1) if first_entry_price else 0.0
        min_close_exc = (min_candle_close / first_entry_price - 1) if first_entry_price else 0.0

        def fmt_ind(candle, keys):
            if candle is None:
                return "n/a"
            parts = []
            for k in keys:
                v = candle.get(k)
                if v is not None and pd.notna(v):
                    parts.append(f"{k}={v:.4f}")
            return " | ".join(parts) if parts else "n/a"

        entry_inds = fmt_ind(entry_candle, ["rubberband_z", "rocp_20", "exhaustion", "atr_14", "btc_mom_avg"])
        exit_inds = fmt_ind(exit_candle, ["rubberband_z", "rocp_5", "atr_14", "btc_mom_avg"])

        # Log tradizionale (completo, per file di log)
        logger.info("=" * 70)
        logger.info(f"[TRADE #{trade.id}] {pair}")
        logger.info(f"  ENTRY : {trade.open_date_utc} | price={first_entry_price:.6f} | tag={trade.enter_tag}")
        logger.info(f"          ind@entry: {entry_inds}")
        logger.info(f"  EXIT  : {curr_time} | price={current_rate:.6f} | reason={exit_reason}")
        logger.info(f"          profit={current_profit:+.2%} leveraged ({price_profit:+.2%} price) | lev={leverage}x")
        logger.info(f"          ind@exit:  {exit_inds}")
        logger.info(f"  RANGE : min={min_price:.6f} ({min_exc:+.2%}) | max={max_price:.6f} ({max_exc:+.2%})")
        logger.info(f"  DCA   : {n_dca} @ [{dca_prices}] | hold={hold_hours:.1f}h")
        logger.info("=" * 70)

        # Messaggio Telegram: escursioni + indicatori (non duplica exit notification Freqtrade)
        msg = (
            f"📊 <b>ESCURSIONI TRADE #{trade.id}</b>\n\n"
            f"<b>Pair:</b> {pair}\n"
            f"<b>Exit reason:</b> {exit_reason}\n\n"
            f"<b>📈 MAX</b>\n"
            f"• Da prima entry: {max_price:.6f} ({max_exc:+.2%})\n"
            f"• Da prezzo medio: {max_price:.6f} ({max_exc_avg:+.2%})\n"
            f"• Chiusura candela max: {max_candle_close:.6f} ({max_close_exc:+.2%}) @ {max_candle_time.strftime('%m-%d %H:%M')}\n\n"
            f"<b>📉 MIN</b>\n"
            f"• Da prima entry: {min_price:.6f} ({min_exc:+.2%})\n"
            f"• Da prezzo medio: {min_price:.6f} ({min_exc_avg:+.2%})\n"
            f"• Chiusura candela min: {min_candle_close:.6f} ({min_close_exc:+.2%}) @ {min_candle_time.strftime('%m-%d %H:%M')}\n\n"
            f"<b>📊 INDICATORI</b>\n"
            f"• Entry: {entry_inds}\n"
            f"• Exit: {exit_inds}\n"
        )
        if n_dca > 0:
            msg += f"\n<b>🔄 DCA:</b> {n_dca} @ [{dca_prices}]\n"
            msg += f"<b>Prezzo medio:</b> {avg_entry_price:.6f}\n"
        
        msg += f"\n⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        
        self._send_telegram(msg)

        # Cleanup set tracking
        self._breakeven_activated.discard(trade.id)
        self._trailing_activated.discard(trade.id)

        self._trade_records.append({
            "id": trade.id,
            "pair": pair,
            "exit_reason": exit_reason,
            "n_dca": n_dca,
            "profit": current_profit,
            "price_profit": price_profit,
            "hold_hours": hold_hours,
            "max_exc": max_exc,
            "min_exc": min_exc,
        })

    # ========================================================================
    # SEZIONE 13: CUSTOM STOPLOSS - LEVERAGE-AGNOSTIC (v6.2.4.17)
    # ========================================================================

    def custom_stoploss(self, pair, trade, current_time, current_rate,
                        current_profit, after_fill, **kwargs):
        """
        Stoploss con trailing dinamico on-exchange + catastrophe in PREZZO.
        """
        leverage = trade.leverage or 1.0

        catastrophe_leveraged = self.CATASTROPHE_PRICE_THRESHOLD * leverage

        if hasattr(self, '_trail_prices') and trade.id in self._trail_prices:
            trail_level = self._trail_prices[trade.id]

            if current_rate > 0:
                profit_ratio = (trail_level / current_rate - 1.0) * leverage
                return max(profit_ratio, catastrophe_leveraged)

        return catastrophe_leveraged

    # ========================================================================
    # SEZIONE 14: CONFIRM TRADE ENTRY
    # ========================================================================

    def confirm_trade_entry(self, pair, order_type, amount, rate, time_in_force,
                            current_time, entry_tag, side, **kwargs):
        if self._is_daily_circuit_breaker_active(current_time):
            return False

        if self._is_weekly_circuit_breaker_active(current_time):
            return False

        max_open_trades = self.config.get("max_open_trades", self.MAX_OPEN_TRADES)
        open_trades = Trade.get_open_trades()

        if len(open_trades) >= max_open_trades:
            return False

        return True

    # ========================================================================
    # SEZIONE 15: CIRCUIT BREAKERS + NOTIFICA TELEGRAM
    # ========================================================================

    def _get_wallet_snapshot(self, current_time: datetime, period: str) -> float:
        if period == "daily":
            period_start = current_time.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        else:
            days_since_monday = current_time.weekday()
            period_start = (
                current_time - timedelta(days=days_since_monday)
            ).replace(hour=0, minute=0, second=0, microsecond=0)

        cache_key = f"{period}_{period_start.isoformat()}"

        if not hasattr(self, "_wallet_snapshot"):
            self._wallet_snapshot = {}

        if cache_key not in self._wallet_snapshot:
            self._wallet_snapshot[cache_key] = self.wallets.get_total(
                self.config["stake_currency"]
            )

        return self._wallet_snapshot.get(
            cache_key,
            self.wallets.get_total(self.config["stake_currency"]),
        )

    def _is_daily_circuit_breaker_active(self, current_time: datetime) -> bool:
        trades = Trade.get_trades_proxy(pair=None, is_open=False)
        if not trades:
            return False

        today_start = current_time.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        curr_time = _ensure_utc(current_time)

        daily_pnl = sum(
            t.close_profit_abs or 0.0
            for t in trades
            if t.close_date_utc
            and t.close_date_utc >= today_start
            and t.close_date_utc <= curr_time
        )

        wallet_start = self._get_wallet_snapshot(current_time, "daily")
        is_active = daily_pnl < -wallet_start * self.DAILY_LOSS_LIMIT_PCT
        
        # Notifica cambio stato circuit breaker (solo su transizione)
        if is_active != self._circuit_breaker_active.get("daily", False):
            if is_active:
                msg = (
                    f"🚨 <b>CIRCUIT BREAKER GIORNALIERO ATTIVATO</b>\n\n"
                    f"<b>PnL oggi:</b> {daily_pnl:+.2f} USDT\n"
                    f"<b>Wallet start:</b> {wallet_start:.2f} USDT\n"
                    f"<b>Limite:</b> -{self.DAILY_LOSS_LIMIT_PCT:.1%} ({-wallet_start * self.DAILY_LOSS_LIMIT_PCT:.2f} USDT)\n"
                    f"\n⛔ <b>Entry bloccate</b>\n"
                    f"\n⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                )
            else:
                msg = (
                    f"✅ <b>CIRCUIT BREAKER GIORNALIERO DISATTIVATO</b>\n\n"
                    f"<b>PnL oggi:</b> {daily_pnl:+.2f} USDT\n"
                    f"\n🟢 <b>Entry riabilitate</b>\n"
                    f"\n⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                )
            self._send_telegram(msg)
            self._circuit_breaker_active["daily"] = is_active
        
        return is_active

    def _is_weekly_circuit_breaker_active(self, current_time: datetime) -> bool:
        trades = Trade.get_trades_proxy(pair=None, is_open=False)
        if not trades:
            return False

        days_since_monday = current_time.weekday()
        week_start = (
            current_time - timedelta(days=days_since_monday)
        ).replace(hour=0, minute=0, second=0, microsecond=0)
        curr_time = _ensure_utc(current_time)

        weekly_pnl = sum(
            t.close_profit_abs or 0.0
            for t in trades
            if t.close_date_utc
            and t.close_date_utc >= week_start
            and t.close_date_utc <= curr_time
        )

        wallet_start = self._get_wallet_snapshot(current_time, "weekly")
        is_active = weekly_pnl < -wallet_start * self.WEEKLY_LOSS_LIMIT_PCT
        
        # Notifica cambio stato circuit breaker (solo su transizione)
        if is_active != self._circuit_breaker_active.get("weekly", False):
            if is_active:
                msg = (
                    f"🚨 <b>CIRCUIT BREAKER SETTIMANALE ATTIVATO</b>\n\n"
                    f"<b>PnL settimana:</b> {weekly_pnl:+.2f} USDT\n"
                    f"<b>Wallet start:</b> {wallet_start:.2f} USDT\n"
                    f"<b>Limite:</b> -{self.WEEKLY_LOSS_LIMIT_PCT:.1%} ({-wallet_start * self.WEEKLY_LOSS_LIMIT_PCT:.2f} USDT)\n"
                    f"\n⛔ <b>Entry bloccate</b>\n"
                    f"\n⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                )
            else:
                msg = (
                    f"✅ <b>CIRCUIT BREAKER SETTIMANALE DISATTIVATO</b>\n\n"
                    f"<b>PnL settimana:</b> {weekly_pnl:+.2f} USDT\n"
                    f"\n🟢 <b>Entry riabilitate</b>\n"
                    f"\n⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                )
            self._send_telegram(msg)
            self._circuit_breaker_active["weekly"] = is_active
        
        return is_active

    # ========================================================================
    # SEZIONE 16: LEVERAGE
    # ========================================================================

    def leverage(self, pair, current_time, current_rate, proposed_leverage,
                 max_leverage, entry_tag, side, **kwargs):
        """
        Leva finanziaria: sempre 3x (scelta utente, non parte della logica 62417).
        """
        return 3.0
