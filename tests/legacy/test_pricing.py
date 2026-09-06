"""Тести курсу, які фіксують поточну поведінку `legacy/pricing.py`.

Цей файл не змінюється. Він потрібен для ЛР3: після вашого рефакторингу всі
чотири тести мають лишитись зеленими без жодної правки, і саме це доводить,
що поведінка модуля не поїхала.

Повним набором тестів це не є навмисно. Граничні випадки ви дописуєте самі
на ЛР4.
"""

from legacy.pricing import calculate_order_total


def test_small_local_order_has_no_discount_and_no_delivery():
    items = [{"price": 100, "quantity": 2}]

    assert calculate_order_total(items, "regular", None, "local") == 200.0


def test_vip_order_above_thousand_gets_discount_and_free_national_delivery():
    items = [{"price": 300, "quantity": 4}]

    assert calculate_order_total(items, "vip", None, "national") == 1020.0


def test_ten_items_in_a_line_get_bulk_discount():
    items = [{"price": 50, "quantity": 10}]

    assert calculate_order_total(items, "regular", None, "national") == 535.0


def test_international_delivery_is_added_to_the_total():
    items = [{"price": 100, "quantity": 3}]

    assert calculate_order_total(items, "regular", None, "international") == 450.0
