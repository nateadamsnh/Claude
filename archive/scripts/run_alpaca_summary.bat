@echo off
cd /d "C:\Users\Nathaniel\Documents\Trading"
python alpaca_daily_summary.py >> "C:\Users\Nathaniel\Documents\Trading\logs\alpaca_summary.log" 2>&1
