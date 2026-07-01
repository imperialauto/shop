@echo off
cd /d "C:\Users\proga\OneDrive\Documents\Claude\Projects\Dashboard"
git add -A
git commit -m "Fix: swap anthropic import to groq in ai_assist.py"
git push
echo.
echo Done! Press any key to close.
pause > nul
