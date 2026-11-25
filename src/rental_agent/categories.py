"""Expense categories: the Schedule E lines, plus the types the owner adds on the Expenses page.

An owner's type (for example hoa_dues) is stored in the expense_categories table. On the tax
report it is part of Schedule E line 19 "Other", listed by name.
"""

import re

import psycopg

# Schedule E (Form 1040) expense lines, in form order.
SCHEDULE_E_CATEGORIES = [
    "advertising", "auto_travel", "cleaning_maintenance", "commissions", "insurance",
    "legal_professional", "management_fees", "mortgage_interest", "other_interest",
    "repairs", "supplies", "taxes", "utilities", "depreciation", "other",
]

_NAME = re.compile(r"[a-z][a-z0-9_]{1,39}")


def load(conn: psycopg.Connection) -> list[str]:
    """Every category an expense can have: the Schedule E lines, then the owner's types."""
    return SCHEDULE_E_CATEGORIES + [
        name for (name,) in conn.execute("select name from expense_categories order by name")]


def add(conn: psycopg.Connection, label: str) -> str:
    """Add an owner's type from what they typed ("HOA dues" is stored as hoa_dues). Returns
    the stored name, or raises ValueError. The caller commits."""
    name = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    if not _NAME.fullmatch(name):
        raise ValueError("a type name needs 2 to 40 letters or digits and must start with "
                         "a letter")
    added = name not in SCHEDULE_E_CATEGORIES and conn.execute(
        "insert into expense_categories (name) values (%s) on conflict (name) do nothing",
        (name,)).rowcount
    if not added:
        raise ValueError(f"the type '{name.replace('_', ' ')}' already exists")
    return name
