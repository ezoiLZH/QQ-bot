@echo off
cd /d "%~dp0"
pip install -r requirements.txt
python setup_mcp.py
if errorlevel 1 echo MCP setup failed - Bilibili search may be unavailable.
echo Installation complete.
pause