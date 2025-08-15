BonicBot — NVS Generator & WebUSB Flasher
=========================================

Setup
-----
1) Python deps:
   pip install flask esp-idf-nvs-partition-gen esptool pyserial

2) Put your firmware 'firmware.bin' into static/ and adjust static/manifest.json if needed.

Run
---
python app.py
Open http://localhost:8001

Usage
-----
- Use "Generate & Download" to get a .bin NVS file.
- Use "Generate & Flash" to generate and then flash the NVS via WebUSB (ESP Web Tools).
- The right-side "Install Firmware (manifest.json)" button flashes a full firmware defined in static/manifest.json.

Notes
-----
- WebUSB requires Chrome/Edge, served over http://localhost or https://.
- Server-side serial flashing is removed; manual CLI command is shown for reference.
