"""The statistics the experiment engine needs, with nothing but `math`.

There is no scipy here, so every one of these is written out. That is a feature rather than a
compromise: the brief's central demand is that the agent never present a coin flip as a finding, and
the only way to be sure of that is for the arithmetic behind "significant" to be readable.

Everything is two-sided and reported with an interval, because an effect size without one invites
exactly the over-claiming this module exists to prevent.

Checked against values worked by hand, in `tests/ppc/test_m8_experiments.py`:
`normal_cdf(1.96) = 0.9750`, `normal_quantile(0.975) = 1.9600`, `t_cdf(2.228, 10) = 0.9750`,
`required_per_arm(0.10, +20%) = 3841`, and a two-proportion test on 0.10 against 0.14 at n=1000:
pooled p = 0.12, standard error sqrt(0.12 x 0.88 x 2/1000) = 0.014533, z = 2.752, two-sided
p = 0.0059.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

SQRT2 = math.sqrt(2.0)


def normal_cdf(z: float) -> float:
    """P(Z <= z) for a standard normal, from the error function."""
    return 0.5 * (1.0 + math.erf(z / SQRT2))


def normal_quantile(p: float, *, tolerance: float = 1e-10) -> float:
    """The z with P(Z <= z) = p, by bisection.

    Bisection rather than a rational approximation on purpose: it is obviously correct from the
    definition, and a few dozen iterations of a cheap function is not a cost worth optimising.
    """
    if not 0.0 < p < 1.0:
        raise ValueError("a quantile needs a probability strictly between 0 and 1")
    low, high = -40.0, 40.0
    while high - low > tolerance:
        middle = (low + high) / 2.0
        if normal_cdf(middle) < p:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


# ---------------- Student's t, for comparing means of few units ----------------
def _log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _betacf(a: float, b: float, x: float, *, iterations: int = 300) -> float:
    """The continued fraction for the incomplete beta function (Lentz's method)."""
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = tiny if abs(d) < tiny else d
    d = 1.0 / d
    h = d
    for m in range(1, iterations + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = tiny if abs(d) < tiny else d
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = tiny if abs(d) < tiny else d
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        step = d * c
        h *= step
        if abs(step - 1.0) < 1e-12:
            break
    return h


def incomplete_beta(a: float, b: float, x: float) -> float:
    """The regularised incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(a * math.log(x) + b * math.log(1.0 - x) - _log_beta(a, b))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - math.exp(b * math.log(1.0 - x) + a * math.log(x) - _log_beta(b, a)) * \
        _betacf(b, a, 1.0 - x) / b


def t_cdf(t: float, df: float) -> float:
    """P(T <= t) for Student's t with `df` degrees of freedom."""
    if df <= 0:
        raise ValueError("degrees of freedom must be positive")
    x = df / (df + t * t)
    tail = 0.5 * incomplete_beta(df / 2.0, 0.5, x)
    return 1.0 - tail if t > 0 else tail


def t_quantile(p: float, df: float, *, tolerance: float = 1e-10) -> float:
    if not 0.0 < p < 1.0:
        raise ValueError("a quantile needs a probability strictly between 0 and 1")
    low, high = -200.0, 200.0
    while high - low > tolerance:
        middle = (low + high) / 2.0
        if t_cdf(middle, df) < p:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


# ---------------- the two tests the engine uses ----------------
@dataclass
class Comparison:
    """One treatment-versus-control answer, with everything needed to judge it."""

    metric: str
    treatment: float
    control: float
    difference: float
    relative: float | None
    ci_low: float
    ci_high: float
    p_value: float
    test: str
    n_treatment: float
    n_control: float
    note: str | None = None

    @property
    def significant(self) -> bool:
        """Significant means the interval excludes no effect — not that the number looks big."""
        return self.ci_low > 0 or self.ci_high < 0

    def as_dict(self) -> dict[str, Any]:
        return {"metric": self.metric, "treatment": round(self.treatment, 6),
                "control": round(self.control, 6), "difference": round(self.difference, 6),
                "relative": None if self.relative is None else round(self.relative, 4),
                "confidence_interval": [round(self.ci_low, 6), round(self.ci_high, 6)],
                "p_value": round(self.p_value, 5), "test": self.test,
                "n_treatment": self.n_treatment, "n_control": self.n_control,
                "significant": self.significant, "note": self.note}


def two_proportions(treatment_successes: int, treatment_trials: int, control_successes: int,
                    control_trials: int, *, metric: str = "cvr",
                    confidence: float = 0.95) -> Comparison | None:
    """Conversion rate in one arm against the other: a two-sided two-proportion z test.

    The interval is unpooled (each arm's own variance) while the p-value is pooled under the null,
    which is the standard pairing — the test asks "could these come from one rate?", the interval
    asks "how big is the difference?", and they are not the same question.
    """
    if treatment_trials <= 0 or control_trials <= 0:
        return None
    p1 = treatment_successes / treatment_trials
    p2 = control_successes / control_trials
    difference = p1 - p2
    pooled = (treatment_successes + control_successes) / (treatment_trials + control_trials)
    null_se = math.sqrt(pooled * (1 - pooled) * (1 / treatment_trials + 1 / control_trials))
    if null_se == 0:
        p_value = 1.0
    else:
        z = difference / null_se
        p_value = 2 * (1 - normal_cdf(abs(z)))
    se = math.sqrt(p1 * (1 - p1) / treatment_trials + p2 * (1 - p2) / control_trials)
    critical = normal_quantile(1 - (1 - confidence) / 2)
    return Comparison(metric=metric, treatment=p1, control=p2, difference=difference,
                      relative=(difference / p2) if p2 else None,
                      ci_low=difference - critical * se, ci_high=difference + critical * se,
                      p_value=min(p_value, 1.0), test="two-proportion z",
                      n_treatment=treatment_trials, n_control=control_trials)


def mean_difference(treatment: list[float], control: list[float], *, metric: str,
                    confidence: float = 0.95) -> Comparison | None:
    """Welch's t test on per-unit values, for anything that is not a proportion.

    Welch rather than Student because the arms will not have equal variance — a treatment that
    changes bids changes the spread as well as the middle, and assuming otherwise overstates
    certainty exactly where it matters.
    """
    if len(treatment) < 2 or len(control) < 2:
        return None
    n1, n2 = len(treatment), len(control)
    m1, m2 = sum(treatment) / n1, sum(control) / n2
    v1 = sum((x - m1) ** 2 for x in treatment) / (n1 - 1)
    v2 = sum((x - m2) ** 2 for x in control) / (n2 - 1)
    se = math.sqrt(v1 / n1 + v2 / n2)
    difference = m1 - m2
    if se == 0:
        return Comparison(metric=metric, treatment=m1, control=m2, difference=difference,
                          relative=(difference / m2) if m2 else None, ci_low=difference,
                          ci_high=difference, p_value=1.0, test="welch t", n_treatment=n1,
                          n_control=n2, note="every unit gave the same value, so there is no spread "
                                             "to test against")
    df = (v1 / n1 + v2 / n2) ** 2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
    t = difference / se
    p_value = 2 * (1 - t_cdf(abs(t), df))
    critical = t_quantile(1 - (1 - confidence) / 2, df)
    return Comparison(metric=metric, treatment=m1, control=m2, difference=difference,
                      relative=(difference / m2) if m2 else None,
                      ci_low=difference - critical * se, ci_high=difference + critical * se,
                      p_value=min(max(p_value, 0.0), 1.0), test="welch t", n_treatment=n1,
                      n_control=n2)


# ---------------- power, before and after ----------------
def power_two_proportions(baseline: float, treated: float, n_per_arm: float, *,
                          alpha: float = 0.05) -> float:
    """The chance of detecting a real difference this size with this much traffic.

    Reported after the fact as achieved power, which is the number that turns "we found nothing"
    into either "there is probably nothing there" or "we never had the traffic to tell".
    """
    if n_per_arm <= 0 or not 0 < baseline < 1 or not 0 <= treated <= 1:
        return 0.0
    difference = abs(treated - baseline)
    if difference == 0:
        return alpha
    pooled = (baseline + treated) / 2
    null_se = math.sqrt(2 * pooled * (1 - pooled) / n_per_arm)
    alt_se = math.sqrt((baseline * (1 - baseline) + treated * (1 - treated)) / n_per_arm)
    if null_se == 0 or alt_se == 0:
        return 0.0
    critical = normal_quantile(1 - alpha / 2) * null_se
    return float(min(max(1 - normal_cdf((critical - difference) / alt_se)
                         + normal_cdf((-critical - difference) / alt_se), 0.0), 1.0))


def required_per_arm(baseline: float, relative_effect: float, *, alpha: float = 0.05,
                     power: float = 0.8) -> int | None:
    """How many trials each arm needs to see a relative change of this size."""
    if not 0 < baseline < 1 or relative_effect == 0:
        return None
    treated = min(max(baseline * (1 + relative_effect), 1e-9), 1 - 1e-9)
    z_alpha = normal_quantile(1 - alpha / 2)
    z_power = normal_quantile(power)
    pooled = (baseline + treated) / 2
    numerator = (z_alpha * math.sqrt(2 * pooled * (1 - pooled))
                 + z_power * math.sqrt(baseline * (1 - baseline) + treated * (1 - treated))) ** 2
    return int(math.ceil(numerator / (treated - baseline) ** 2))


def detectable_effect(baseline: float, n_per_arm: float, *, alpha: float = 0.05,
                      power: float = 0.8, tolerance: float = 1e-4) -> float | None:
    """The smallest relative change this much traffic could find — the honest design question.

    Asked before an experiment runs. If the answer is "only a 60% improvement", the experiment is
    not worth running, and saying so beforehand is cheaper than three weeks of spend followed by
    "inconclusive".
    """
    if n_per_arm <= 0 or not 0 < baseline < 1:
        return None
    low, high = 0.0, 5.0
    if power_two_proportions(baseline, min(baseline * (1 + high), 1 - 1e-9), n_per_arm,
                             alpha=alpha) < power:
        return None
    while high - low > tolerance:
        middle = (low + high) / 2
        reached = power_two_proportions(baseline, min(baseline * (1 + middle), 1 - 1e-9),
                                        n_per_arm, alpha=alpha)
        if reached < power:
            low = middle
        else:
            high = middle
    return round((low + high) / 2, 4)
