"""Tiny demo module with an intentional bug for Helix to find and fix.

``divide`` currently lets a ZeroDivisionError escape. The desired behavior is a
clear ValueError so callers get a meaningful message.
"""


def divide(a: float, b: float) -> float:
    return a / b


def mean(values: list[float]) -> float:
    return sum(values) / len(values)
