# Sample data

`synthetic_nifty_5m.csv` is **synthetic, randomly generated data** (10 weekday
sessions × 75 five-minute candles). It is *not* NIFTY market data and must not be
used to draw conclusions about any strategy. Regenerate with
`python scripts/generate_sample_data.py`.

Its `volume` column imitates a traded-volume series purely so the relative-volume
code path can be exercised.

Real market data belongs in `data/raw/` or `data/local/`, which are git-ignored.
