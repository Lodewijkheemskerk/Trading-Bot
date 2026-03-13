# Trading Formulas Reference

## Market Edge
```
edge = p_model - p_market
```
Only trade when `edge > 0.04` (4%).

## Expected Value (EV)
```
EV = p * b - (1 - p)
```
- `p` = model probability, `b` = decimal odds − 1

## Mispricing Score (Z-Score)
```
delta = (p_model - p_market) / std_dev
```
- δ > 2.0 = strong signal
- δ > 1.5 = moderate
- δ < 1.0 = weak (skip)

## Brier Score (Calibration)
```
BS = (1/n) * Σ(predicted - outcome)²
```
Lower is better. Target < 0.25.

## Kelly Criterion (Position Sizing)
```
f* = (p * b - q) / b
```
- `f*` = fraction of bankroll to bet
- `p` = win probability, `q` = 1 − p, `b` = net odds
- **Always use Fractional Kelly:** multiply by 0.25 (quarter) to 0.50 (half)

### Kelly Example
Bankroll $10,000 | Win prob 70% | Odds 2:1
- Full Kelly: 12% ($1,200)
- Quarter-Kelly: 3% ($300) ← use this

## Sharpe Ratio
```
Sharpe = (R_p - R_f) / σ_p
```
Target > 2.0.

## Value at Risk (VaR 95%)
```
VaR_95 = μ - 1.645 * σ
```

## Profit Factor
```
Profit Factor = Gross Profit / Gross Loss
```
Target > 1.5.

## Performance Targets
| Metric | Target | Danger |
|--------|--------|--------|
| Win Rate | >60% | <50% |
| Sharpe | >2.0 | <1.0 |
| Max Drawdown | <8% | >8% (stop) |
| Profit Factor | >1.5 | <1.0 |
| Brier Score | <0.25 | >0.30 |
| Daily Loss | <15% | >15% (stop) |
