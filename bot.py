"""Compatibility entry point.

Deployment platforms still run ``python bot.py``; the implementation lives in
the :mod:`bot` package.
"""

from bot.main import main

if __name__ == "__main__":
    main()
