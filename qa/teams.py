"""Team membership defaults.

Managers set each agent's team on the Settings page; that stored value is
what counts. These defaults only decide the starting team the first time an
agent appears (from CXone, an assignment, a screening or the import).
"""

CUSTOMER_SUPPORT = "customer_support"
INTERNAL_SUPPORT = "internal_support"
TEAMS = (CUSTOMER_SUPPORT, INTERNAL_SUPPORT)

# Full names that start in Internal Support.
_INTERNAL_FULL_NAMES = {"lisa gilmore", "lisa farnsworth"}
# First names that start in Internal Support (only one person each today).
_INTERNAL_FIRST_NAMES = {"melissa", "jodi", "stacie", "caitlyn"}


def name_key(name: str) -> str:
    """Lower-case, single-spaced: how the same person is matched everywhere."""
    return " ".join((name or "").lower().split())


def default_team(name: str) -> str:
    key = name_key(name)
    if key in _INTERNAL_FULL_NAMES:
        return INTERNAL_SUPPORT
    first = key.split(" ", 1)[0] if key else ""
    if first in _INTERNAL_FIRST_NAMES:
        return INTERNAL_SUPPORT
    return CUSTOMER_SUPPORT
