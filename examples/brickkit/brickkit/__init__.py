"""Shared Python code that can be built into a wheel and imported in notebooks."""


def normalize_name(value: str) -> str:
    return " ".join(value.strip().split()).title()


def revenue(quantity: int, unit_price: float) -> float:
    return round(quantity * unit_price, 2)
