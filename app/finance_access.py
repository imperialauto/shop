"""Who may open Finances: the claimed owner account only.

Matches app.permissions, where /finance is OWNER_ONLY. Kept as its own small
check so every finance route and export re-verifies it, even if the global
role gate changes. No other role, and no configured list of logins, qualifies.
"""


def is_finance_owner(user) -> bool:
    return user is not None and getattr(user, "role", None) == "owner"
