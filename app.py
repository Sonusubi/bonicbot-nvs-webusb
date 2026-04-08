from flask import Flask, request, render_template, send_file, jsonify
import subprocess
import csv
import tempfile
import os
import json
import hashlib
from datetime import datetime, timedelta
import serial.tools.list_ports
import requests
from threading import Thread, Lock
import time

# --- GitHub Firmware Configuration ---
FIRMWARE_REPOS = {
    "BonicBotS1": {
        "owner": "Autobonics",
        "repo": "bonicbot-firmware-mainPCB",
        "asset_name": "mainPCB.bin"
    },
    "BonicBotA1": {
        "owner": "Autobonics",
        "repo": "bonicbota1-firmware-pcb",
        "asset_name": "mainPCB.bin"
    },
    "BonicBotA2": {
        "owner": "Autobonics",
        "repo": "bonicbota2-firmware-pcb",
        "asset_name": "mainPCB.bin"
    }
}
FIRMWARE_ASSET_NAME = "mainPCB.bin"

app = Flask(__name__, static_url_path='/static', static_folder='static')
app.secret_key = 'bonicbot_nvs_generator_2024'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

# Global firmware manager instances
firmware_managers = {}
firmware_lock = Lock()

class FirmwareManager:
    def __init__(self, bot_name, repo_owner, repo_name, asset_name):
        self.bot_name = bot_name
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.asset_name = asset_name
        self.static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', bot_name)
        self.firmware_path = os.path.join(self.static_dir, asset_name)
        self.metadata_path = os.path.join(self.static_dir, 'firmware_metadata.json')
        self.check_interval = 3600  # Check every hour (in seconds)
        self.last_check_time = 0
        self.current_version = None
        self.is_checking = False
        
        # Load existing metadata
        self._load_metadata()
        
        # Background checker removed as per user request

    
    def _load_metadata(self):
        """Load firmware metadata from local file."""
        try:
            if os.path.exists(self.metadata_path):
                with open(self.metadata_path, 'r') as f:
                    metadata = json.load(f)
                    self.current_version = metadata.get('version')
                    self.last_check_time = metadata.get('last_check', 0)
                    return metadata
        except Exception as e:
            print(f"⚠️  Warning: Could not load firmware metadata for {self.bot_name}: {e}")
        return {}
    
    def _save_metadata(self, metadata):
        """Save firmware metadata to local file."""
        try:
            os.makedirs(self.static_dir, exist_ok=True)
            with open(self.metadata_path, 'w') as f:
                json.dump(metadata, f, indent=2)
        except Exception as e:
            print(f"⚠️  Warning: Could not save firmware metadata for {self.bot_name}: {e}")
    
    def _get_file_hash(self, file_path):
        """Calculate SHA256 hash of a file."""
        try:
            with open(file_path, 'rb') as f:
                return hashlib.sha256(f.read()).hexdigest()
        except:
            return None
    
    def get_latest_release_info(self):
        """Get latest release information from GitHub API."""
        if self.repo_owner in ["your_github_username"] and self.repo_name == "your_github_repository":
            return None, "Repository configuration not set"
        
        api_url = f"https://api.github.com/repos/{self.repo_owner}/{self.repo_name}/releases/latest"
        
        try:
            response = requests.get(api_url, timeout=15)
            response.raise_for_status()
            release_data = response.json()
            
            # Find the firmware asset
            asset_url = None
            asset_size = None
            for asset in release_data.get('assets', []):
                if asset['name'] == self.asset_name:
                    asset_url = asset['browser_download_url']
                    asset_size = asset['size']
                    break
            
            if not asset_url:
                return None, f"Firmware asset '{self.asset_name}' not found in latest release"
            
            return {
                'version': release_data['tag_name'],
                'published_at': release_data['published_at'],
                'download_url': asset_url,
                'size': asset_size,
                'release_notes': release_data.get('body', ''),
                'prerelease': release_data.get('prerelease', False)
            }, None
            
        except requests.exceptions.RequestException as e:
            return None, f"Network error: {e}"
        except Exception as e:
            return None, f"Unexpected error: {e}"
    
    def get_all_releases_info(self):
        """Get all releases information from GitHub API."""
        if self.repo_owner in ["your_github_username"] and self.repo_name == "your_github_repository":
            return None, "Repository config not set"
        
        api_url = f"https://api.github.com/repos/{self.repo_owner}/{self.repo_name}/releases"
        try:
            response = requests.get(api_url, timeout=15)
            response.raise_for_status()
            releases_data = response.json()
            
            releases = []
            for release in releases_data:
                tag_name = release.get('tag_name')
                asset_url = None
                asset_size = None
                for asset in release.get('assets', []):
                    if asset['name'] == self.asset_name:
                        asset_url = asset['browser_download_url']
                        asset_size = asset['size']
                        break
                if asset_url:
                    releases.append({
                        'version': tag_name,
                        'published_at': release.get('published_at'),
                        'download_url': asset_url,
                        'size': asset_size,
                        'release_notes': release.get('body', ''),
                        'prerelease': release.get('prerelease', False)
                    })
            return releases, None
        except Exception as e:
            return None, f"Error: {e}"

    def needs_update(self):
        """Check if firmware needs updating."""
        # Skip if we've checked recently
        current_time = time.time()
        if current_time - self.last_check_time < self.check_interval:
            return False, "Recently checked", None
        
        release_info, error = self.get_latest_release_info()
        if error:
            return False, error, None
        
        # Update last check time
        self.last_check_time = current_time
        
        # If no local firmware exists, we need to download
        if not os.path.exists(self.firmware_path):
            return True, "No local firmware found", release_info
        
        # If we don't know the current version, assume we need update
        if not self.current_version:
            return True, "Unknown local version", release_info
        
        # Compare versions
        if self.current_version != release_info['version']:
            return True, f"New version available: {release_info['version']} (current: {self.current_version})", release_info
        
        return False, "Up to date", release_info
    
    def download_firmware(self, release_info=None):
        """Download firmware with version tracking."""
        with firmware_lock:
            if self.is_checking:
                return False, "Download already in progress"
            self.is_checking = True
        
        try:
            versions_dir = os.path.join(self.static_dir, 'versions')
            os.makedirs(versions_dir, exist_ok=True)
            if not release_info:
                return False, "Cannot proceed without target version"
                
            v_name = release_info['version']
            target_bin = os.path.join(versions_dir, f"{v_name}.bin")
            
            # Download the specific release if we don't have it fully cached offline
            if not (os.path.exists(target_bin) and os.path.getsize(target_bin) == release_info.get('size', -1)):
                print(f"🚀 Downloading firmware version {v_name} into cache...")
                try:
                    response = requests.get(release_info['download_url'], stream=True, timeout=60)
                    response.raise_for_status()
                    
                    downloaded_size = 0
                    with open(target_bin + '.tmp', 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                downloaded_size += len(chunk)
                                
                    if downloaded_size == release_info.get('size', downloaded_size):
                        os.rename(target_bin + '.tmp', target_bin)
                    else:
                        os.unlink(target_bin + '.tmp')
                        return False, f"Incomplete download for {v_name}"
                        
                except Exception as e:
                    return False, f"Failed to download {v_name}: {e}"
            else:
                print(f"⚡ Using offline cached version {v_name} for activation...")
            
            if os.path.exists(target_bin):
                import shutil
                shutil.copy2(target_bin, self.firmware_path)
                
                # Calculate hash for integrity
                file_hash = self._get_file_hash(self.firmware_path)
                
                # Update metadata
                metadata = {
                    'version': release_info['version'],
                    'downloaded_at': datetime.now().isoformat(),
                    'last_check': time.time(),
                    'size': release_info.get('size', os.path.getsize(self.firmware_path)),
                    'hash': file_hash,
                    'release_notes': release_info.get('release_notes', ''),
                    'prerelease': release_info.get('prerelease', False)
                }
                self._save_metadata(metadata)
                self.current_version = release_info['version']
                
                msg = f"Activated latest firmware version {release_info['version']}."
                return True, msg
            else:
                return False, f"Target binary {release_info['version']} could not be found."
                
        except Exception as e:
            return False, f"Download failed: {e}"
        finally:
            with firmware_lock:
                self.is_checking = False
    
    # Background checker method removed

    
    def get_status(self):
        """Get current firmware status."""
        status = {
            'local_firmware_exists': os.path.exists(self.firmware_path),
            'current_version': self.current_version,
            'last_check': self.last_check_time,
            'is_checking': self.is_checking,
            'repo': f"{self.repo_owner}/{self.repo_name}",
            'asset_name': self.asset_name
        }
        
        if os.path.exists(self.firmware_path):
            stat = os.stat(self.firmware_path)
            status['file_size'] = stat.st_size
            status['file_modified'] = stat.st_mtime
            
        # Append locally downloaded versions for offline swapping
        versions_dir = os.path.join(self.static_dir, 'versions')
        offline_versions = []
        if os.path.exists(versions_dir):
            for f in os.listdir(versions_dir):
                if f.endswith('.bin'):
                    offline_versions.append(f.replace('.bin', ''))
        status['offline_versions'] = sorted(offline_versions, reverse=True)
        
        return status

def initialize_firmware_managers():
    """Initialize the global firmware managers."""
    global firmware_managers
    for bot_name, config in FIRMWARE_REPOS.items():
        firmware_managers[bot_name] = FirmwareManager(
            bot_name,
            config["owner"],
            config["repo"],
            config["asset_name"]
        )
        
        # Perform initial firmware check (Local only)
        print(f"🔍 Initialized firmware manager for {bot_name} (Current: {firmware_managers[bot_name].current_version})")

# ---------- New Firmware API Routes ----------

@app.route('/api/firmware/<bot_name>/status')
def firmware_status(bot_name):
    """Get current firmware status."""
    try:
        if bot_name not in firmware_managers:
            return jsonify({'error': f'Invalid bot name: {bot_name}'}), 404
        
        status = firmware_managers[bot_name].get_status()
        return jsonify(status)
    except Exception as e:
        return jsonify({'error': f'Status check failed: {str(e)}'}), 500

@app.route('/api/firmware/<bot_name>/check-update', methods=['POST'])
def check_firmware_update(bot_name):
    """Manually trigger firmware update check."""
    try:
        if bot_name not in firmware_managers:
            return jsonify({'error': f'Invalid bot name: {bot_name}'}), 404
        
        manager = firmware_managers[bot_name]
        # Force check by resetting last check time
        manager.last_check_time = 0
        
        needs_update, reason, release_info = manager.needs_update()
        
        # Fetch all available releases to show dropdown
        all_releases, error = manager.get_all_releases_info()
        
        result = {
            'needs_update': needs_update,
            'reason': reason,
            'current_version': manager.current_version,
            'available_versions': all_releases if all_releases else []
        }
        
        if release_info:
            result['latest_version'] = release_info['version']
            result['release_notes'] = release_info['release_notes']
            result['published_at'] = release_info['published_at']
            result['prerelease'] = release_info['prerelease']
        
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'error': f'Update check failed: {str(e)}'}), 500

@app.route('/api/firmware/<bot_name>/download', methods=['POST'])
def download_firmware(bot_name):
    """Manually trigger firmware download."""
    try:
        if bot_name not in firmware_managers:
            return jsonify({'error': f'Invalid bot name: {bot_name}'}), 404
            
        manager = firmware_managers[bot_name]
        if manager.is_checking:
            return jsonify({'error': 'Download already in progress'}), 409
        
        # Check if caller wants a specific version
        data = request.json or {}
        target_version = data.get('version')
        
        # If offline swap available, do it INSTANTLY without touching GitHub to prevent rate limits
        versions_dir = os.path.join(manager.static_dir, 'versions')
        offline_target_path = os.path.join(versions_dir, f"{target_version}.bin") if target_version else None
        
        if offline_target_path and os.path.exists(offline_target_path):
            import shutil
            shutil.copy2(offline_target_path, manager.firmware_path)
            
            # Update metadata
            m = manager._load_metadata()
            m['version'] = target_version
            manager._save_metadata(m)
            manager.current_version = target_version
            
            return jsonify({'success': True, 'message': f'Swapped instantly to local cached version: {target_version}'})
            
        release_info = None
        if target_version:
            all_releases, _ = manager.get_all_releases_info()
            if all_releases:
                for r in all_releases:
                    if r['version'] == target_version:
                        release_info = r
                        break
            if not release_info:
                return jsonify({'error': f'Version {target_version} not found in releases or offline cache'}), 404
        
        success, message = manager.download_firmware(release_info)
        
        if success:
            return jsonify({'success': True, 'message': message})
        else:
            return jsonify({'success': False, 'error': message}), 500
            
    except Exception as e:
        return jsonify({'error': f'Download failed: {str(e)}'}), 500

# ---------- Original Routes (unchanged) ----------

@app.route('/')
def index():
    return render_template('index.html', bots=list(FIRMWARE_REPOS.keys()))

@app.route('/api/list-ports')
def list_ports():
    """List available serial ports with ESP32 device filtering."""
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
    """Check if required tools are available on the server."""
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
    # Initialize firmware managers
    initialize_firmware_managers()
    
    print("🤖 BonicBot NVS Generator (Enhanced Firmware Management)")
    print("📊 UI: http://localhost:8001")
    print("🔧 Install: pip install -r requirements.txt")
    print("📡 Firmware API endpoints:")
    for bot_name in FIRMWARE_REPOS.keys():
        print(f"   /{bot_name}:")
        print(f"     GET  /api/firmware/{bot_name}/status - Get firmware status")
        print(f"     POST /api/firmware/{bot_name}/check-update - Check for updates")
        print(f"     POST /api/firmware/{bot_name}/download - Download latest firmware")
    
    app.run(host='0.0.0.0', port=8001,ssl_context=('/home/pi/certs/raspberrypi.crt', '/home/pi/certs/raspberrypi.key'), debug=True)
