from flask import Flask, request, render_template, send_file, jsonify
import subprocess
import csv
import tempfile
import os
from datetime import datetime
import serial.tools.list_ports

app = Flask(__name__)
app.secret_key = 'bonicbot_nvs_generator_2024'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

# ---------- Routes ----------

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/list-ports')
def list_ports():
    """List available serial ports with ESP32 device filtering (for manual CLI info only)."""
    try:
        ports = []
        available_ports = serial.tools.list_ports.comports()

        for port in available_ports:
            port_info = {
                'device': port.device,
                'description': (port.description or 'Unknown Device'),
                'hwid': (port.hwid or ''),
                'manufacturer': getattr(port, 'manufacturer', '') or '',
                'vid': getattr(port, 'vid', None),
                'pid': getattr(port, 'pid', None)
            }

            esp32_s3_indicators = [
                ('vid', 0x303A),  # Espressif VID
                ('vid', 0x10C4),  # Silicon Labs (CP210x)
                ('vid', 0x1A86),  # QinHeng (CH340)
                ('vid', 0x0403),  # FTDI
                ('description', 'cp210x'),
                ('description', 'ch340'),
                ('description', 'ch341'),
                ('description', 'esp32'),
                ('description', 'silicon labs'),
                ('hwid', 'cp210x'),
                ('hwid', 'ch340'),
                ('hwid', 'ch341')
            ]

            is_esp_device = False
            confidence_score = 0

            for check_type, indicator in esp32_s3_indicators:
                if check_type == 'vid' and isinstance(indicator, int):
                    if port_info['vid'] == indicator:
                        is_esp_device = True
                        confidence_score += 10
                elif check_type in ['description', 'hwid']:
                    field_value = str(port_info.get(check_type, '')).lower()
                    if isinstance(indicator, str) and indicator.lower() in field_value:
                        is_esp_device = True
                        confidence_score += 5

            # Enhanced description for ESP devices
            desc_lower = (port_info['description'] or '').lower()
            if is_esp_device:
                if port_info['vid'] == 0x303A:
                    port_info['esp_type'] = 'ESP32-S3 (Native USB)'
                elif 'cp210x' in desc_lower:
                    port_info['esp_type'] = 'ESP32 (CP210x Bridge)'
                elif 'ch340' in desc_lower or 'ch341' in desc_lower:
                    port_info['esp_type'] = 'ESP32 (CH34x Bridge)'
                else:
                    port_info['esp_type'] = 'ESP32 Compatible'

                port_info['display_name'] = f"{port_info['device']} - {port_info['esp_type']}"
            else:
                port_info['esp_type'] = 'Generic Device'
                port_info['display_name'] = f"{port_info['device']} - {port_info['description']}"

            port_info['is_esp_device'] = is_esp_device
            port_info['confidence'] = confidence_score
            ports.append(port_info)

        ports.sort(key=lambda x: (not x['is_esp_device'], -x['confidence'], x['device']))

        return jsonify({
            'ports': ports,
            'esp_count': len([p for p in ports if p['is_esp_device']]),
            'total_count': len(ports)
        })
    except Exception as e:
        return jsonify({'error': f'Port detection failed: {str(e)}', 'ports': []}), 500

def _generate_nvs_bin(csv_rows):
    """Generate a temporary NVS .bin from given CSV rows. Returns bin_path."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as csv_file:
        writer = csv.writer(csv_file)
        writer.writerows(csv_rows)
        csv_path = csv_file.name

    bin_path = csv_path.replace('.csv', '.bin')

    # Try multiple methods to call the generator
    methods = [
        ['python3', '-m', 'esp_idf_nvs_partition_gen.nvs_partition_gen', 'generate', csv_path, bin_path, '0x4000'],
        ['python', '-m', 'esp_idf_nvs_partition_gen.nvs_partition_gen', 'generate', csv_path, bin_path, '0x4000'],
        ['nvs_partition_gen', 'generate', csv_path, bin_path, '0x4000'],
    ]

    last_err = ""
    for cmd in methods:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0 and os.path.exists(bin_path):
                # cleanup CSV after success
                try: os.unlink(csv_path)
                except: pass
                return bin_path
            else:
                last_err = f"{' '.join(cmd)} -> rc={result.returncode}, out={result.stdout}, err={result.stderr}"
        except Exception as e:
            last_err = f"{' '.join(cmd)} raised {e}"

    # If we get here, all methods failed
    try: os.unlink(csv_path)
    except: pass
    raise RuntimeError(f"NVS generation failed. Details: {last_err}")

@app.route('/generate-single', methods=['POST'])
def generate_single():
    """Generate single NVS binary file and send it as attachment."""
    try:
        device_id = (request.form.get('device_id') or '').strip()

        if not device_id:
            return jsonify({'error': 'Device ID is required'}), 400

        csv_content = [
            ['key', 'type', 'encoding', 'value'],
            ['bonicbot', 'namespace', '', ''],
            ['device_id', 'data', 'string', device_id]
        ]

        bin_path = _generate_nvs_bin(csv_content)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'{device_id}_nvs_{timestamp}.bin'
        return send_file(bin_path, as_attachment=True, download_name=filename, mimetype='application/octet-stream')
    except Exception as e:
        return jsonify({'error': f'Server error: {str(e)}'}), 500

@app.route('/api/validate-tools')
def validate_tools():
    """Check if required tools are available on the server (for NVS generation; esptool is optional)."""
    tools_status = {}
    methods = [
        ('esp_idf_nvs_partition_gen', ['python3', '-m', 'esp_idf_nvs_partition_gen.nvs_partition_gen', '--help']),
        ('esp_idf_nvs_partition_gen_py', ['python', '-m', 'esp_idf_nvs_partition_gen.nvs_partition_gen', '--help']),
        ('nvs_partition_gen_global', ['nvs_partition_gen', '--help']),
        ('esptool', ['esptool.py', '--help']),
    ]

    for name, cmd in methods:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            tools_status[name] = (result.returncode == 0)
        except Exception:
            tools_status[name] = False

    return jsonify(tools_status)

if __name__ == '__main__':
    print("🤖 BonicBot NVS Generator (WebUSB flashing via ESP Web Tools)")
    print("📊 UI: http://localhost:8001")
    print("🔧 Install: pip install esp-idf-nvs-partition-gen esptool pyserial flask")
    app.run(host='0.0.0.0', port=8001, debug=True)
