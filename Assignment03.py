"""
MSDS 451 - Assignment 03
Bollinger Band auto-trading strategy on the top 10 XBI holdings, backtested with Backtrader.

Steps
-----
1) Ingest daily OHLCV data from yfinance for the top 10 XBI holdings plus SPY.
2) Compute Bollinger Bands (20-day rolling lookback, 2 standard deviation multiplier).
3) Backtest a long-only Bollinger Band mean-reversion strategy with Backtrader,
   benchmarked against buy-and-hold SPY.

Strategy rules (per stock)
--------------------------
- Entry: close crosses below the lower band  -> buy (target 1/N of portfolio equity).
- Exit:  close crosses above the middle band (20-day SMA) -> sell the whole position.
         (Set EXIT_BAND = "upper" to hold until the upper band instead.)

Usage
-----
    pip install yfinance backtrader pandas matplotlib
    python Assignment03.py
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import backtrader as bt
import matplotlib
import matplotlib.ticker

matplotlib.use("Agg")  # write plots to file, no GUI needed
import matplotlib.pyplot as plt
import pandas as pd
import yfinance as yf

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
XBI_TOP10 = ["MRNA", "EXEL", "HALO", "REGN", "AMGN", "BIIB", "UTHR", "GILD", "ABBV", "NTRA"]
BENCHMARK = "SPY"
TICKERS = XBI_TOP10 + [BENCHMARK]

START_DATE = "2019-01-01"  # MRNA IPO'd Dec 2018, so all 10 names have full history from here
END_DATE = dt.date.today().isoformat()

BB_PERIOD = 20        # rolling lookback (trading days)
BB_DEVFACTOR = 2.0    # standard deviation multiplier
EXIT_BAND = "mid"     # "mid" (20-day SMA) or "upper"

STARTING_CASH = 100_000.0
COMMISSION = 0.001    # 10 bps per trade

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
DATA_DIR = Path(__file__).resolve().parent / "data"


# ----------------------------------------------------------------------------
# 1) Data ingestion
# ----------------------------------------------------------------------------
def download_data(tickers: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """Download split/dividend-adjusted daily OHLCV for each ticker and cache to CSV."""
    DATA_DIR.mkdir(exist_ok=True)
    raw = yf.download(
        tickers,
        start=start,
        end=end,
        auto_adjust=True,
        group_by="ticker",
        progress=False,
        threads=True,
    )

    data: dict[str, pd.DataFrame] = {}
    for t in tickers:
        if t not in raw.columns.get_level_values(0):
            print(f"  WARNING: no data returned for {t}; skipping")
            continue
        df = raw[t][["Open", "High", "Low", "Close", "Volume"]].dropna().copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        df.index.name = "Date"
        df.to_csv(DATA_DIR / f"{t}.csv")
        data[t] = df
        print(f"  {t:5s}: {len(df):5d} rows  {df.index[0].date()} -> {df.index[-1].date()}")
    return data


# ----------------------------------------------------------------------------
# 2) Bollinger Bands (pandas version, for inspection / export)
# ----------------------------------------------------------------------------
def add_bollinger_bands(df: pd.DataFrame, period: int = BB_PERIOD,
                        devfactor: float = BB_DEVFACTOR) -> pd.DataFrame:
    """Append BB_Mid / BB_Upper / BB_Lower / %B and raw crossing signals to a price frame."""
    out = df.copy()
    mid = out["Close"].rolling(period).mean()
    # ddof=0 (population std) matches Backtrader's BollingerBands indicator
    std = out["Close"].rolling(period).std(ddof=0)
    out["BB_Mid"] = mid
    out["BB_Upper"] = mid + devfactor * std
    out["BB_Lower"] = mid - devfactor * std
    out["BB_PctB"] = (out["Close"] - out["BB_Lower"]) / (out["BB_Upper"] - out["BB_Lower"])

    prev_close = out["Close"].shift(1)
    exit_line = out["BB_Mid"] if EXIT_BAND == "mid" else out["BB_Upper"]
    out["BuySignal"] = (out["Close"] < out["BB_Lower"]) & (prev_close >= out["BB_Lower"].shift(1))
    out["SellSignal"] = (out["Close"] > exit_line) & (prev_close <= exit_line.shift(1))
    return out


# ----------------------------------------------------------------------------
# 3) Backtrader strategy
# ----------------------------------------------------------------------------
class BollingerBandStrategy(bt.Strategy):
    """Long-only Bollinger Band mean reversion across multiple data feeds."""

    params = dict(
        period=BB_PERIOD,
        devfactor=BB_DEVFACTOR,
        exit_band=EXIT_BAND,
        target_pct=None,   # fraction of equity per position; default = 0.98 / N
        printlog=False,
    )

    def __init__(self):
        n = len(self.datas)
        self.target_pct = self.p.target_pct or 0.98 / n  # keep a small cash buffer
        self.bbands = {}
        self.entry_cross = {}
        self.exit_cross = {}
        self.orders = {}
        self.fills = []  # executed orders, used to plot actual trades
        for d in self.datas:
            bb = bt.indicators.BollingerBands(d.close, period=self.p.period, devfactor=self.p.devfactor)
            exit_line = bb.mid if self.p.exit_band == "mid" else bb.top
            self.bbands[d] = bb
            # CrossDown(close, bot) == 1 when close moves from above to below the lower band
            self.entry_cross[d] = bt.indicators.CrossDown(d.close, bb.bot)
            self.exit_cross[d] = bt.indicators.CrossUp(d.close, exit_line)
            self.orders[d] = None

    def log(self, txt: str, d=None):
        if self.p.printlog:
            date = (d or self.datas[0]).datetime.date(0)
            print(f"{date} {txt}")

    def notify_order(self, order):
        if order.status in (order.Submitted, order.Accepted):
            return
        d = order.data
        if order.status == order.Completed:
            side = "BUY " if order.isbuy() else "SELL"
            self.fills.append({"Date": d.datetime.datetime(0), "Ticker": d._name, "Side": side.strip(),
                               "Size": order.executed.size, "Price": order.executed.price,
                               "Commission": order.executed.comm})
            self.log(f"{side} {d._name:5s} size={order.executed.size:8.0f} "
                     f"price={order.executed.price:9.2f} comm={order.executed.comm:7.2f}", d)
        elif order.status in (order.Canceled, order.Margin, order.Rejected):
            self.log(f"ORDER {order.getstatusname()} {d._name}", d)
        self.orders[d] = None

    def notify_trade(self, trade):
        if trade.isclosed:
            self.log(f"CLOSE {trade.data._name:5s} pnl gross={trade.pnl:9.2f} net={trade.pnlcomm:9.2f}",
                     trade.data)

    def next(self):
        for d in self.datas:
            if self.orders[d] is not None:
                continue  # wait for pending order on this name
            pos = self.getposition(d).size
            if not pos and self.entry_cross[d][0] > 0:
                self.orders[d] = self.order_target_percent(data=d, target=self.target_pct)
            elif pos and self.exit_cross[d][0] > 0:
                self.orders[d] = self.close(data=d)

    def stop(self):
        self.log(f"Ending portfolio value {self.broker.getvalue():,.2f}")


class BuyAndHold(bt.Strategy):
    """Benchmark: invest everything in the single data feed on day one and hold."""

    def next(self):
        if not self.position:
            self.order_target_percent(target=0.99)


def build_cerebro(strategy, feeds: dict[str, pd.DataFrame], **strat_kwargs) -> bt.Cerebro:
    cerebro = bt.Cerebro()
    cerebro.broker.setcash(STARTING_CASH)
    cerebro.broker.setcommission(commission=COMMISSION)
    for name, df in feeds.items():
        cerebro.adddata(bt.feeds.PandasData(dataname=df, name=name), name=name)
    cerebro.addstrategy(strategy, **strat_kwargs)

    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Days,
                        annualize=True, riskfreerate=0.0)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns", tann=252)
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(bt.analyzers.TimeReturn, _name="timereturn", timeframe=bt.TimeFrame.Days)
    return cerebro


def summarize(label: str, strat, start_value: float) -> dict:
    end_value = strat.broker.getvalue()
    sharpe = strat.analyzers.sharpe.get_analysis().get("sharperatio")
    dd = strat.analyzers.drawdown.get_analysis()
    rets = strat.analyzers.returns.get_analysis()
    trades = strat.analyzers.trades.get_analysis()

    total_closed = trades.get("total", {}).get("closed", 0) if trades else 0
    won = trades.get("won", {}).get("total", 0) if total_closed else 0
    summary = {
        "Strategy": label,
        "Start Value": start_value,
        "End Value": end_value,
        "Total Return %": (end_value / start_value - 1) * 100,
        "CAGR %": rets.get("rnorm100"),
        "Sharpe (ann.)": sharpe,
        "Max Drawdown %": dd.max.drawdown,
        "Closed Trades": total_closed,
        "Win Rate %": (won / total_closed * 100) if total_closed else None,
    }
    return summary


def per_ticker_trade_stats(strat) -> pd.DataFrame:
    """Aggregate closed-trade P&L by ticker using Backtrader's trade records."""
    rows = []
    for d in strat.datas:
        for trade in strat._trades[d][0]:
            if trade.isclosed:
                rows.append({"Ticker": d._name, "PnL": trade.pnlcomm, "Bars": trade.barlen})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return (df.groupby("Ticker")
              .agg(Trades=("PnL", "size"), NetPnL=("PnL", "sum"),
                   WinRate=("PnL", lambda s: (s > 0).mean() * 100), AvgBarsHeld=("Bars", "mean"))
              .sort_values("NetPnL", ascending=False))


def plot_equity(strategy_returns: pd.Series, bench_returns: pd.Series, path: Path):
    """Equity curve of the strategy vs. buy-and-hold SPY, with a drawdown panel underneath."""
    colors = {"Bollinger Band (XBI Top 10)": "#1f77b4", "Buy & Hold SPY": "#7f7f7f"}
    eq = pd.DataFrame({
        "Bollinger Band (XBI Top 10)": (1 + strategy_returns).cumprod() * STARTING_CASH,
        "Buy & Hold SPY": (1 + bench_returns).cumprod() * STARTING_CASH,
    })
    drawdown = (eq / eq.cummax() - 1) * 100

    fig, (ax, ax_dd) = plt.subplots(2, 1, figsize=(12, 7.5), sharex=True,
                                    gridspec_kw={"height_ratios": [3, 1]})
    for col in eq.columns:
        ax.plot(eq.index, eq[col], label=col, color=colors[col], linewidth=1.5)
        final = eq[col].iloc[-1]
        ax.annotate(f"${final:,.0f}", xy=(eq.index[-1], final), xytext=(5, 0),
                    textcoords="offset points", va="center", color=colors[col], fontsize=9)
        ax_dd.fill_between(drawdown.index, drawdown[col], 0, color=colors[col], alpha=0.35, label=col)

    ax.axhline(STARTING_CASH, color="black", linewidth=0.8, linestyle="--", alpha=0.5)
    ax.set_title(f"Equity Curve: Bollinger Band ({BB_PERIOD}d, {BB_DEVFACTOR:g}σ) on XBI Top 10 vs. Buy & Hold SPY")
    ax.set_ylabel("Portfolio Value ($)")
    ax.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("${x:,.0f}"))
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)

    ax_dd.set_ylabel("Drawdown (%)")
    ax_dd.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_bands_grid(bb_frames: dict[str, pd.DataFrame], tickers: list[str], path: Path,
                    fills: pd.DataFrame, lookback: int | None = 252):
    """Consolidated 5x2 grid of every stock's price, Bollinger Bands and executed trades.

    fills: executed orders from the backtest (Date, Ticker, Side, Price).
    lookback: number of trading days to show (None = full history).
    """
    ncols = 2
    nrows = -(-len(tickers) // ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(16, 3.2 * nrows), squeeze=False)

    for ax, t in zip(axes.flat, tickers):
        df = bb_frames[t] if lookback is None else bb_frames[t].iloc[-lookback:]
        ax.fill_between(df.index, df["BB_Lower"], df["BB_Upper"], color="#1f77b4", alpha=0.15)
        ax.plot(df.index, df["BB_Upper"], color="#1f77b4", linewidth=0.7)
        ax.plot(df.index, df["BB_Lower"], color="#1f77b4", linewidth=0.7)
        ax.plot(df.index, df["BB_Mid"], color="#ff7f0e", linewidth=0.9, linestyle="--")
        ax.plot(df.index, df["Close"], color="black", linewidth=1.0)
        f = fills[(fills["Ticker"] == t) & (fills["Date"] >= df.index[0])]
        buys, sells = f[f["Side"] == "BUY"], f[f["Side"] == "SELL"]
        ms = 28 if lookback else 14
        ax.scatter(buys["Date"], buys["Price"], marker="^", color="green", s=ms, zorder=3)
        ax.scatter(sells["Date"], sells["Price"], marker="v", color="red", s=ms, zorder=3)
        pct_b = df["BB_PctB"].iloc[-1]
        ax.set_title(f"{t}   close ${df['Close'].iloc[-1]:,.2f}   %B {pct_b:.2f}", fontsize=10, loc="left")
        ax.grid(alpha=0.3)
        ax.tick_params(labelsize=8)

    for ax in list(axes.flat)[len(tickers):]:
        ax.set_visible(False)

    handles = [
        plt.Line2D([], [], color="black", linewidth=1.0, label="Close"),
        plt.Line2D([], [], color="#ff7f0e", linestyle="--", label=f"{BB_PERIOD}d SMA"),
        plt.Rectangle((0, 0), 1, 1, color="#1f77b4", alpha=0.25, label=f"±{BB_DEVFACTOR:g}σ band"),
        plt.Line2D([], [], marker="^", color="green", linestyle="", label="Executed buy"),
        plt.Line2D([], [], marker="v", color="red", linestyle="", label="Executed sell"),
    ]
    window = "full history" if lookback is None else f"last {lookback} trading days"
    fig.suptitle(f"XBI Top 10 Holdings: Bollinger Bands ({BB_PERIOD}d, {BB_DEVFACTOR:g}σ), {window}",
                 fontsize=14)
    fig.legend(handles=handles, loc="upper center", ncol=5, bbox_to_anchor=(0.5, 0.965), frameon=False)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_bands(df: pd.DataFrame, ticker: str, path: Path):
    tail = df.iloc[-252:]  # last ~1 year for readability
    fig, ax = plt.subplots(figsize=(11, 5.5))
    ax.plot(tail.index, tail["Close"], label="Close", color="black", linewidth=1.2)
    ax.plot(tail.index, tail["BB_Mid"], label=f"{BB_PERIOD}d SMA", linewidth=1)
    ax.fill_between(tail.index, tail["BB_Lower"], tail["BB_Upper"], alpha=0.15, label=f"±{BB_DEVFACTOR:g}σ band")
    buys = tail[tail["BuySignal"]]
    sells = tail[tail["SellSignal"]]
    ax.scatter(buys.index, buys["Close"], marker="^", color="green", s=60, label="Buy signal", zorder=3)
    ax.scatter(sells.index, sells["Close"], marker="v", color="red", s=60, label="Exit signal", zorder=3)
    ax.set_title(f"{ticker} Bollinger Bands (last 252 trading days)")
    ax.legend(loc="best")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    OUTPUT_DIR.mkdir(exist_ok=True)

    print(f"[1] Downloading data {START_DATE} -> {END_DATE}")
    data = download_data(TICKERS, START_DATE, END_DATE)
    if BENCHMARK not in data:
        raise RuntimeError("Benchmark data (SPY) could not be downloaded.")
    universe = {t: data[t] for t in XBI_TOP10 if t in data}

    print(f"\n[2] Computing Bollinger Bands ({BB_PERIOD}-day lookback, {BB_DEVFACTOR:g} SD)")
    bb_frames = {t: add_bollinger_bands(df) for t, df in data.items()}
    for t, df in bb_frames.items():
        df.to_csv(OUTPUT_DIR / f"{t}_bollinger.csv")
    latest = pd.DataFrame({
        t: df.iloc[-1][["Close", "BB_Lower", "BB_Mid", "BB_Upper", "BB_PctB"]]
        for t, df in bb_frames.items()
    }).T
    latest.columns.name = None
    latest["Signal Count (Buy)"] = [int(bb_frames[t]["BuySignal"].sum()) for t in latest.index]
    print("Latest band levels:")
    print(latest.round(3).to_string())
    for t in bb_frames:
        plot_bands(bb_frames[t], t, OUTPUT_DIR / f"{t}_bands.png")

    print("\n[3] Running Backtrader backtest")
    cerebro = build_cerebro(BollingerBandStrategy, universe, printlog=False)
    strat = cerebro.run()[0]

    fills = pd.DataFrame(strat.fills, columns=["Date", "Ticker", "Side", "Size", "Price", "Commission"])
    fills.to_csv(OUTPUT_DIR / "trade_log.csv", index=False)
    plot_bands_grid(bb_frames, list(universe), OUTPUT_DIR / "xbi_top10_bands_grid.png", fills)
    plot_bands_grid(bb_frames, list(universe), OUTPUT_DIR / "xbi_top10_bands_grid_full.png", fills,
                    lookback=None)
    strat_summary = summarize("Bollinger Band (XBI Top 10)", strat, STARTING_CASH)

    bench_cerebro = build_cerebro(BuyAndHold, {BENCHMARK: data[BENCHMARK]})
    bench = bench_cerebro.run()[0]
    bench_summary = summarize("Buy & Hold SPY", bench, STARTING_CASH)

    results = pd.DataFrame([strat_summary, bench_summary]).set_index("Strategy")
    print("\nPerformance summary:")
    print(results.round(2).T.to_string())
    results.to_csv(OUTPUT_DIR / "performance_summary.csv")

    ticker_stats = per_ticker_trade_stats(strat)
    if not ticker_stats.empty:
        print("\nClosed-trade P&L by ticker:")
        print(ticker_stats.round(2).to_string())
        ticker_stats.to_csv(OUTPUT_DIR / "trades_by_ticker.csv")

    strat_rets = pd.Series(strat.analyzers.timereturn.get_analysis())
    bench_rets = pd.Series(bench.analyzers.timereturn.get_analysis())
    strat_rets.index = pd.to_datetime(strat_rets.index)
    bench_rets.index = pd.to_datetime(bench_rets.index)
    plot_equity(strat_rets, bench_rets, OUTPUT_DIR / "equity_curve.png")

    print(f"\nOutputs written to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
