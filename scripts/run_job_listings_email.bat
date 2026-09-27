@echo off
cd /d "C:\Users\Nathaniel\Documents\Trading"
python scripts\job_listings_email.py >> "C:\Users\Nathaniel\Documents\Trading\logs\job_listings_email.log" 2>&1
