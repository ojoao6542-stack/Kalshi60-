"""Signal, sizing and online calibration. Prices and risk caps are in dollars."""
import math
from decimal import Decimal, ROUND_CEILING

MIN_EDGE = 0.12
MIN_TRAIN = 30
BLOCK = 40
CONFIDENCE = 0.60


def clamp(x, low, high):
    return max(low, min(high, x))


def probability(baseline, weights, features):
    if weights is None:
        return baseline
    logit = math.log(baseline / (1 - baseline))
    correction = sum(w * x for w, x in zip(weights, features))
    return 1 / (1 + math.exp(-clamp(logit + correction, -8, 8)))


def update(weights, seen, base, x, yes):
    # Each labeled observation is consumed once, in settlement order.
    error = yes - probability(base, weights, x)
    rate = 0.18 / math.sqrt(1 + seen / 50)
    return [clamp(w + rate * (error * v - 0.015 * w), -3, 3)
            for w, v in zip(weights, x)]


def signal(samples, spot, strike, seconds, yes, no):
    # Use elapsed times. Ignore history before the latest interruption.
    tail = []
    for ts, price in samples:
        if tail and ts - tail[-1][0] > 40:
            tail.clear()
        if not tail or ts > tail[-1][0]:
            tail.append((ts, price))
    if len(tail) < 20 or tail[-1][0] - tail[0][0] < 150:
        return None
    elapsed = tail[-1][0] - tail[0][0]
    variance_rate = sum(math.log(b[1] / a[1]) ** 2 for a, b in zip(tail, tail[1:])) / elapsed
    if variance_rate < 1e-15:
        return None
    z = math.log(spot / strike) / math.sqrt(variance_rate * seconds)
    base = clamp(0.5 * (1 + math.erf(z / math.sqrt(2))), 0.01, 0.99)
    earlier = next(p for t, p in tail if t >= tail[-1][0] - 60)
    trend = math.log(spot / earlier) / math.sqrt(60 * variance_rate)
    mid = (yes + 1 - no) / 2
    return base, [1.0, clamp(z, -3, 3), 2 * mid - 1, clamp(trend, -3, 3), seconds / 900]


def chart_forecast(candles, end):
    """Three hours of consecutive finalized five-minute bars, as known at end."""
    bars = {}
    for row in candles:
        ts = int(row[0])
        if ts < end - 10800 or ts >= end:
            continue
        op, close = float(row[3]), float(row[4])
        if all(math.isfinite(v) and v > 0 for v in (op, close)):
            bars[ts] = (op, close)
    required = range(end - 10800, end, 300)
    if any(t not in bars for t in required):
        return None
    prices = [bars[end - 10800][0]] + [bars[t][1] for t in required]
    moves = [math.log(b/a) for a,b in zip(prices,prices[1:])]
    rms = math.sqrt(sum(v*v for v in moves)/36)
    returns = [math.log(prices[-1]/prices[-1-n]) for n in (3,12,36)]
    # Trend-continuation estimate per five minutes, capped for sudden moves.
    drift = sum(w*r/n for w,r,n in zip((0.5,0.3,0.2),returns,(3,12,36)))
    drift = clamp(drift, -0.5*rms, 0.5*rms)
    direction = "MIXED"
    if rms > 0 and abs(drift) >= 0.10*rms and drift*returns[0] > 0:
        direction = "UP" if drift > 0 else "DOWN"
    return dict(end=end, direction=direction, drift=drift,
                changes=[round(100*math.expm1(v),3) for v in returns])


def chart_side(chart, spot, strike, seconds):
    projected = spot * math.exp(chart["drift"]*seconds/300)
    if chart["direction"] == "UP" and projected >= strike:
        return "YES"
    if chart["direction"] == "DOWN" and projected < strike:
        return "NO"
    return None


def cost(price, n):
    p = Decimal(str(price))
    fee = (Decimal("0.07") * n * p * (1 - p)).quantize(Decimal(".01"), rounding=ROUND_CEILING)
    return float(n * p + fee), float(fee)


def entry(p_yes, quotes, cap, allowed=("YES","NO")):
    choices = []
    for side, p in (("YES", p_yes), ("NO", 1 - p_yes)):
        if side not in allowed:
            continue
        price, depth = quotes[side]
        n = int(cap / price)
        while n and cost(price, n)[0] > cap:
            n -= 1
        if not n or depth < n:
            continue
        paid, fee = cost(price, n)
        edge = p - paid / n
        if edge >= MIN_EDGE:
            choices.append((edge, dict(side=side, price=price, n=n, paid=paid, fee=fee, confidence=p)))
    return max(choices, key=lambda pair: pair[0])[1] if choices else None


def gains(order, yes):
    if not order:
        return 0
    won = (order["side"] == "YES") == bool(yes)
    return round((order["n"] if won else 0) - order["paid"], 6)


def assess(rows, rival, reference):
    # Identical markets and a $20 simulated budget for each policy.
    orders = [r[rival + "_order"] for r in rows]
    profits = [gains(o, r["yes"]) for o, r in zip(orders, rows)]
    net = sum(profits)
    reference_net = sum(gains(r[reference + "_order"], r["yes"]) for r in rows)
    improvement = sum((r[reference] - r["yes"])**2 - (r[rival] - r["yes"])**2 for r in rows) / len(rows)
    fills = sum(o is not None for o in orders)
    passes = (fills >= 12 and improvement >= 0.005 and net > max(0, reference_net)
              and net - max(profits) > 0)
    return dict(passed=passes, trades=fills, net=round(net, 2), reference_net=round(reference_net, 2),
                brier_improvement=round(improvement, 5))
