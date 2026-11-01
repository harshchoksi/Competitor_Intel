"""Currency display helpers shared by alerts, insights and the PDF."""
from __future__ import annotations

SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "AUD": "A$", "CAD": "C$", "NZD": "NZ$", "SGD": "S$", "HKD": "HK$", "JPY": "¥", "CNY": "¥",
           "AWG": "Afl.", "AED": "AED ", "SAR": "SAR ", "ZAR": "R", "BRL": "R$", "MXN": "MX$", "CHF": "CHF ", "PKR": "Rs ", "BDT": "৳", "LKR": "Rs ",
           "KRW": "₩", "TRY": "₺", "THB": "฿", "PHP": "₱", "IDR": "Rp ", "MYR": "RM", "NPR": "Rs ", "KES": "KSh ", "NGN": "₦", "EGP": "E£"}


def symbol(currency: str | None) -> str:
    c = (currency or "INR").upper()
    return SYMBOLS.get(c, c + " ")


def fmt(value: float | None, currency: str | None = "INR", decimals: int | None = None) -> str:
    """₹1,299 for big INR amounts, $6.27 for small ones. Decimals default to 0 for amounts >= 100, otherwise 2."""
    if value is None:
        return "–"
    d = decimals if decimals is not None else (0 if abs(value) >= 100 else 2)
    return f"{symbol(currency)}{value:,.{d}f}"
