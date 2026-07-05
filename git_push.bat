@echo off
cd /d "C:\Users\proga\OneDrive\Documents\Claude\Projects\Dashboard"
git add -A
git commit -m "Fix SMS send: OPENPHONE_PHONE_NUMBER_ID bypass + full webhook/send logging"
git push
echo.
echo Done! Press any key to close.
pause > nul
