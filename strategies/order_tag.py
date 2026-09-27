#!/usr/bin/env python3
"""
Order tagging — attribute broker orders to a strategy.

No order placed anywhere in this repo previously set client_order_id, so once
more than one strategy can hold positions there'd be no way to tell, at the
broker level, which strategy is responsible for which order. This just builds
that id string; callers pass it in the order payload's "client_order_id" field.
"""

import re
import uuid


def client_order_id(strategy: str, symbol: str) -> str:
    """e.g. client_order_id('wheel', 'MARA') -> 'wheel-MARA-a1b2c3d4'.
    Strips characters Alpaca's client_order_id field won't accept."""
    clean = lambda s: re.sub(r"[^A-Za-z0-9_.-]", "", str(s))
    return f"{clean(strategy)}-{clean(symbol)}-{uuid.uuid4().hex[:8]}"
