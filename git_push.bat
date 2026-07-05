@echo off
cd /d "C:\Users\proga\OneDrive\Documents\Claude\Projects\Dashboard"
git add -A
git commit -m "Fix fleet vehicle dropdown, add VIN decoder + year select, Google Calendar scheduling"
git push
echo.
echo Done! Press any key to close.
pause > nul
