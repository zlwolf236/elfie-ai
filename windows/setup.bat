@echo off
REM One-time setup for the Elfie wake-word listener (run on Windows)
python -m pip install --quiet openwakeword sounddevice numpy onnxruntime
echo.
echo Done. Start listening with:  python wake_listener.py
pause
