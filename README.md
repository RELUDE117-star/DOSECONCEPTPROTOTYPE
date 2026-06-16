# DOSE Home Station

Double-click **DOSE.sh** → pick **"Execute in Terminal"** → done.

First run installs what's needed. Every run after that checks for updates automatically.

Press **Esc** to exit. Tap the **D** button (bottom right) to switch modes.


python3 -c "import board,busio,adafruit_mpr121 as M; m=M.MPR121(busio.I2C(board.SCL,board.SDA)); import time
while 1:
 print([i for i in range(12) if m[i].value]); time.sleep(0.3)"
