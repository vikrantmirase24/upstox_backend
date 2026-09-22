import pandas as pd
import numpy as np

def calculate_heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """Standard OHLC DataFrame ko Heikin-Ashi me transform karta hai"""
    ha_df = df.copy()
    ha_df['ha_close'] = (df['open'] + df['high'] + df['low'] + df['close']) / 4

    ha_open = [df['open'].iloc[0]]
    for i in range(1, len(df)):
        ha_open.append((ha_open[i - 1] + ha_df['ha_close'].iloc[i - 1]) / 2)
    ha_df['ha_open'] = ha_open

    ha_df['ha_high'] = ha_df[['high', 'ha_open', 'ha_close']].max(axis=1)
    ha_df['ha_low'] = ha_df[['low', 'ha_open', 'ha_close']].min(axis=1)
    return ha_df

def calculate_supertrend(df: pd.DataFrame, period=1, multiplier=1.0) -> pd.DataFrame:
    """Heikin-Ashi parameters: Period=1, Multiplier=1.0"""
    df = calculate_heikin_ashi(df)

    # Average True Range (ATR)
    high_low = df['ha_high'] - df['ha_low']
    high_close = np.abs(df['ha_high'] - df['ha_close'].shift())
    low_close = np.abs(df['ha_low'] - df['ha_close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    atr = true_range.rolling(period).mean()

    hl2 = (df['ha_high'] + df['ha_low']) / 2
    upperband = hl2 + (multiplier * atr)
    lowerband = hl2 - (multiplier * atr)

    in_uptrend = [True] * len(df)
    supertrend = [0.0] * len(df)

    for i in range(1, len(df)):
        if df['ha_close'].iloc[i] > upperband.iloc[i - 1]:
            in_uptrend[i] = True
        elif df['ha_close'].iloc[i] < lowerband.iloc[i - 1]:
            in_uptrend[i] = False
        else:
            in_uptrend[i] = in_uptrend[i - 1]

            if in_uptrend[i] and lowerband.iloc[i] < lowerband.iloc[i - 1]:
                lowerband.iloc[i] = lowerband.iloc[i - 1]
            if not in_uptrend[i] and upperband.iloc[i] > upperband.iloc[i - 1]:
                upperband.iloc[i] = upperband.iloc[i - 1]

        supertrend[i] = lowerband.iloc[i] if in_uptrend[i] else upperband.iloc[i]

    df['supertrend'] = supertrend
    df['is_green'] = in_uptrend
    return df