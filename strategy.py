"""Signal, sizing and online calibration. Prices and risk caps are in dollars."""
import math
from decimal import Decimal
from config import MIN_PROBABILITY

MIN_EDGE = 0.12
MIN_TRAIN = 30
BLOCK = 40


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


def forecast(history, quotes, remaining):
    """Experimental continuation of the current contract's quote momentum."""
    if len(history) < 4:
        return None
    elapsed = history[-1][0] - history[0][0]
    if elapsed < 30 or any(b[0]-a[0] > 40 for a,b in zip(history,history[1:])):
        return None
    mid = history[-1][1]
    move = mid-history[0][1]
    drift = clamp(move * min(remaining,120) / elapsed,-.20,.20)
    spread = quotes["YES"][0]+quotes["NO"][0]-1
    return clamp(mid+drift,.01,.99), [1.,2*mid-1,clamp(5*move,-1,1),10*spread,remaining/900]


def entry(p_yes, quotes, cap, allowed=("YES","NO"), minimum=0):
    choices = []
    for side, p in (("YES", p_yes), ("NO", 1 - p_yes)):
        if side not in allowed or p < MIN_PROBABILITY:
            continue
        price, depth = quotes[side]
        unit = Decimal(str(price))
        n = min(depth, int(Decimal(str(cap)) / unit))
        paid = float(unit * n)
        edge = round(p-price, 12)
        if n and paid >= minimum and edge >= MIN_EDGE:
            choices.append((edge, dict(side=side, price=price, n=n, paid=paid, fee=0, confidence=p)))
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
