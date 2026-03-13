# Failure Log

Every loss is analyzed and logged here. The scan and research agents read this
file before processing new markets to avoid repeating past mistakes.

## Failure Categories
1. **Bad Prediction** — Model probability was significantly wrong
2. **Bad Timing** — Right direction, wrong entry/exit timing
3. **Bad Execution** — Slippage, partial fills, API errors
4. **External Shock** — Unpredictable event invalidated thesis

## Entry Format
```
### [DATE] - [MARKET] - [CATEGORY]
- Entry Price:
- Exit Price:
- Model Probability:
- Actual Outcome:
- P&L:
- Root Cause:
- Lesson:
- Action Taken:
```

---

## Entries

*(No failures logged yet — system initializing)*
