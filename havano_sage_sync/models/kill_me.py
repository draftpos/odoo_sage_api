import os
import signal

print("Killing all odoo processes...")
try:
    os.system("pkill -9 -f odoo")
except Exception as e:
    print(e)
