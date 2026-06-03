"""Calculo de descuento por porcentaje y monto fijo."""

from app.services.promotions import _calc_discount


def test_pct_discount():
    assert _calc_discount(10, None, 10000) == 1000  # 10% de $100 = $10
    assert _calc_discount(50, None, 199) == 100     # redondeo


def test_fixed_discount():
    assert _calc_discount(None, 500, 10000) == 500


def test_no_discount_when_both_null():
    assert _calc_discount(None, None, 10000) == 0
