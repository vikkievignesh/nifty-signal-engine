"""Regenerate data/sample/synthetic_nifty_5m.csv (SYNTHETIC, not market data)."""

from pathlib import Path

from nifty_signal_engine.synthetic import generate_synthetic_candles

OUT = Path(__file__).resolve().parents[1] / "data" / "sample" / "synthetic_nifty_5m.csv"

if __name__ == "__main__":
    df = generate_synthetic_candles(n_sessions=10, seed=7)
    df["timestamp"] = df["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%S%z").str.replace(r"(\d{2})(\d{2})$", r"\1:\2", regex=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False)
    print(f"wrote {len(df)} rows to {OUT}")
