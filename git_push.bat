@echo off
cd /d "C:\Users\proga\OneDrive\Documents\Claude\Projects\Dashboard"
git add -A
git commit -m "Fix SMS auth: remove Bearer prefix from OpenPhone API calls (401 fix)"
git push
echo.
echo Done! Press any key to close.
pause > nul
