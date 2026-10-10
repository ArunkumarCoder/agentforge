# ruff: noqa: E501
"""Reference documentation for the Documentation golden cases: what a good answer looks like."""

from typing import Any

REFERENCE_DOCS: dict[str, dict[str, Any]] = {
    "temperature": {
        "docstrings": {
            "<module>": "Convert and parse temperatures in Celsius and Fahrenheit.",
            "celsius_to_fahrenheit": "Convert degrees Celsius to degrees Fahrenheit.\n\nArgs:\n    celsius: Temperature in degrees Celsius.\n\nReturns:\n    The same temperature in degrees Fahrenheit.\n\nExample:\n    >>> celsius_to_fahrenheit(100)\n    212.0",
            "fahrenheit_to_celsius": "Convert degrees Fahrenheit to degrees Celsius.\n\nArgs:\n    fahrenheit: Temperature in degrees Fahrenheit.\n\nReturns:\n    The same temperature in degrees Celsius.\n\nExample:\n    >>> fahrenheit_to_celsius(212)\n    100.0",
            "parse_temperature": "Parse a reading such as '21.5C' or '70 f' into degrees Celsius.\n\nArgs:\n    text: A number followed by a unit, C or F (any case); spaces are allowed.\n\nReturns:\n    The temperature in degrees Celsius.\n\nRaises:\n    ValueError: If text is not a number followed by C or F.\n\nExample:\n    >>> parse_temperature('212F')\n    100.0",
        },
        "readme": '# temperature\n\nConvert between Celsius and Fahrenheit, and parse readings.\n\n```python\nfrom units.temperature import celsius_to_fahrenheit, fahrenheit_to_celsius, parse_temperature\n\ncelsius_to_fahrenheit(100)  # 212.0\nfahrenheit_to_celsius(32)  # 0.0\nparse_temperature("70 F")  # 21.11...\n```\n',
    },
    "retry": {
        "docstrings": {
            "<module>": "Retry a flaky call with exponential backoff.",
            "retry": "Call func until it succeeds, waiting longer after each failure.\n\nThe wait doubles each time (exponential backoff): delay, 2*delay, 4*delay, ...\n\nParameters\n----------\nfunc\n    The call to make. It takes no arguments.\nattempts\n    How many times to try in total. Must be at least 1.\ndelay\n    Seconds to wait after the first failure.\nexceptions\n    Exception types that trigger a retry; anything else is raised at once.\nsleep\n    Function used to wait; replace it in tests.\n\nReturns\n-------\nT\n    Whatever func returns on the first successful call.\n\nRaises\n------\nValueError\n    If attempts is less than 1.\nException\n    The last exception from func if every attempt fails.",
        },
        "readme": "# retry\n\n`retry` calls a function again when it fails, with exponential backoff.\n\n```python\nfrom net.retry import retry\n\nresult = retry(lambda: fetch_status(), attempts=5, delay=0.2, exceptions=(TimeoutError,))\n```\n",
    },
    "bank_account": {
        "docstrings": {
            "<module>": "A simple bank account with deposits and withdrawals.",
            "InsufficientFundsError": "Raised when a withdrawal is larger than the balance.",
            "Account": "A bank account belonging to one owner.\n\n:ivar owner: Name of the account holder.",
            "Account.__init__": "Open an account.\n\n:param owner: Name of the account holder.\n:param balance: Opening balance; defaults to zero.\n:raises ValueError: If the opening balance is negative.",
            "Account.balance": "The current balance.",
            "Account.from_dict": "Create an account from a dict with an 'owner' key and an optional 'balance'.\n\n:param data: Mapping with 'owner' and, optionally, 'balance' as a string.\n:returns: The new account.",
            "Account.deposit": "Add money to the account.\n\n:param amount: Amount to add; must be positive.\n:returns: The new balance.\n:raises ValueError: If amount is not positive.",
            "Account.withdraw": "Take money out of the account.\n\n:param amount: Amount to take out; must be positive.\n:returns: The new balance.\n:raises ValueError: If amount is not positive.\n:raises InsufficientFundsError: If amount is more than the balance.",
        },
        "readme": '# Bank accounts\n\n`Account` tracks one owner\'s balance. Withdrawing more than the balance raises `InsufficientFundsError`.\n\n```python\nfrom decimal import Decimal\n\nfrom bank.account import Account, InsufficientFundsError\n\naccount = Account("Ada", Decimal("10"))\naccount.deposit(Decimal("5"))\ntry:\n    account.withdraw(Decimal("100"))\nexcept InsufficientFundsError:\n    print("not enough money")\n```\n',
    },
    "iter_tools": {
        "docstrings": {
            "<module>": "Helpers that split iterables into chunks and windows.",
            "chunked": "Split items into lists of size items; the last list may be shorter.\n\nArgs:\n    items: Any iterable.\n    size: Number of items per chunk; at least 1.\n\nYields:\n    Lists of up to size items, in order.\n\nRaises:\n    ValueError: If size is less than 1.\n\nExample:\n    >>> list(chunked([1, 2, 3, 4, 5], 2))\n    [[1, 2], [3, 4], [5]]",
            "sliding_window": "Yield overlapping tuples of width consecutive items.\n\nArgs:\n    items: Any iterable.\n    width: Items per window; at least 1.\n\nYields:\n    Tuples of width items, moving one item at a time.\n\nRaises:\n    ValueError: If width is less than 1.\n\nExample:\n    >>> list(sliding_window('abcd', 3))\n    [('a', 'b', 'c'), ('b', 'c', 'd')]",
        },
        "readme": "# iters\n\nSplit iterables into fixed-size chunks or overlapping windows.\n\n```python\nfrom toolkit.iters import chunked, sliding_window\n\nfor batch in chunked(range(10), 4):\n    print(batch)\n\nprint(list(sliding_window([1, 2, 3, 4], 2)))\n```\n",
    },
    "partly_documented": {
        "docstrings": {
            "truncate": "Shorten text to at most limit characters, ending with suffix if cut.\n\nArgs:\n    text: The text to shorten.\n    limit: Maximum length of the result, suffix included.\n    suffix: Added to the end when the text is cut.\n\nReturns:\n    The text unchanged if it fits, otherwise the cut text plus suffix.\n\nRaises:\n    ValueError: If limit is shorter than suffix.\n\nExample:\n    >>> truncate('hello world', 8)\n    'hello...'",
            "initials": "Return the upper-case first letter of each word in a name.\n\nArgs:\n    name: A name such as 'ada  lovelace'.\n\nReturns:\n    The initials, e.g. 'AL'.\n\nExample:\n    >>> initials(' ada  lovelace ')\n    'AL'",
        },
        "readme": "# clean\n\nTidy up text: `squash_spaces`, `truncate` and `initials`.\n\n```python\nfrom text.clean import initials, squash_spaces, truncate\n\nsquash_spaces(\"  a   b \")  # 'a b'\ntruncate(\"hello world\", 8)  # 'hello...'\ninitials(\"ada lovelace\")  # 'AL'\n```\n",
    },
}
