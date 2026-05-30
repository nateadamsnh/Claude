"""
One-time script: buy enough ACI to reach 100 shares for covered call eligibility.
Run: python buy_aci.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "politician-copy-trader"))
from trader import place_buy, get_latest_price, get_position, is_market_open

SYMBOL = "ACI"
TARGET_SHARES = 100

# --- Check market ---
if not is_market_open():
    print("Market is currently CLOSED. Run this script during market hours (Mon-Fri 9:30am-4pm ET).")
    sys.exit(1)

# --- Get current position ---
pos = get_position(SYMBOL)
current_qty = float(pos["qty"]) if pos else 0.0
print(f"Current {SYMBOL} position : {current_qty:.4f} shares")

if current_qty >= TARGET_SHARES:
    print(f"Already at or above {TARGET_SHARES} shares — nothing to do.")
    sys.exit(0)

needed = TARGET_SHARES - current_qty
price  = get_latest_price(SYMBOL)
notional = round(needed * price, 2)

print(f"Current price            : ${price:.2f}")
print(f"Shares needed            : {needed:.4f}")
print(f"Order notional           : ${notional:.2f}")
print()

confirm = input(f"Buy ${notional:.2f} of {SYMBOL} at market? [y/N]: ").strip().lower()
if confirm != "y":
    print("Cancelled.")
    sys.exit(0)

order = place_buy(SYMBOL, notional)
print()
print(f"  Order ID : {order.get('id')}")
print(f"  Status   : {order.get('status')}")
print(f"  Once filled, run 'python strategies/wheel_strategy.py monitor' to confirm 100 shares.")
