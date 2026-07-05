@echo off
cd /d "C:\Users\proga\OneDrive\Documents\Claude\Projects\Dashboard"
git add -A
git commit -m "Add SMS thread UI to RO detail + debug SMS send logging"
git push
echo.
echo Done! Press any key to close.
pause > nul
