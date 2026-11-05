"""Production entry point.

Deployment platforms run ``python main.py``; the implementation lives in the
:mod:`discord_bot` package. ``python -m discord_bot`` is also supported.
"""

from discord_bot.main import main

if __name__ == "__main__":
    main()
