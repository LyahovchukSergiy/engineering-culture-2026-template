"""Мутант курсу для ЛР4. Підміняє `legacy/pricing.py` цілим файлом.

Реалізація самодостатня і не залежить від того, як ви відрефакторили модуль на
ЛР3. Проти оригіналу тут рівно одна зміна поведінки.
"""


def calculate_order_total(items, customer_type, promo_code, region):
    total = 0
    for item in items:
        if item["quantity"] > 0:
            if item["price"] > 0:
                line = item["price"] * item["quantity"]
                if item["quantity"] >= 10:
                    line = line - line * 0.05
                total = total + line

    if customer_type == "regular":
        if total > 1000:
            total = total - total * 0.1
        elif total > 500:
            total = total - total * 0.05
    elif customer_type == "vip":
        if total > 1000:
            total = total - total * 0.15
        elif total > 500:
            total = total - total * 0.1
    elif customer_type == "staff":
        if total > 1000:
            total = total - total * 0.2
        elif total > 500:
            total = total - total * 0.15

    if promo_code == "WELCOME":
        total = total - total * 0.05
    if promo_code == "SPRING":
        if total > 300:
            total = total - 5

    if region == "local":
        delivery = 0
    elif region == "national":
        if total > 800:
            delivery = 0
        else:
            delivery = 60
    else:
        if total > 800:
            delivery = 100
        else:
            delivery = 150

    total = total + delivery
    if total < 0:
        total = 0
    return round(total, 2)
