# Author: RA
# Purpose:  Shared event loop
# Created: 23/09/2025

import asyncio

loop = asyncio.get_event_loop()

def start_loop():
    import threading
    threading.Thread(target=loop.run_forever, daemon=True).start()
