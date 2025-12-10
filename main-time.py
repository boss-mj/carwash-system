"""
Carwash Vendo Machine (Optimized for Raspberry Pi 4)
====================================================
POWERED BY: VENDOPRO x SOLE DEVELOPEMENT
"""

# --- Kivy Graphics Optimizations for Raspberry Pi 4 ---
from kivy.config import Config

Config.set('graphics', 'fullscreen', 'auto')      # auto-detect display
Config.set('kivy', 'keyboard_mode', 'dock')       # enable touch keyboard
Config.set('kivy', 'show_cursor', '0')            # disable mouse cursor
Config.set('graphics', 'maxfps', '0')
Config.set('graphics', 'multisamples', '0')
Config.set('graphics', 'vsync', '1')
Config.set('graphics', 'resizable', '0')
Config.set('kivy', 'exit_on_escape', '1')
Config.set('kivy', 'window', 'sdl2')
Config.set('kivy', 'video', 'ffpyplayer')
Config.set('input', 'mouse', 'mouse,disable_multitouch')

# Ensure ffpyplayer section exists
if not Config.has_section('ffpyplayer'):
    Config.add_section('ffpyplayer')

Config.set('ffpyplayer', 'sync', 'video')
Config.set('ffpyplayer', 'anaglyph', '0')

from kivy.animation import Animation
from kivy.uix.button import Button
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import FadeTransition
from kivy.core.window import Window
from kivy.uix.label import Label

import subprocess
import shutil
import json
import os
import threading
import time
import requests
import logging
import platform
import glob
import tempfile

import firebase_admin
from firebase_admin import credentials, firestore

from kivy.app import App
from kivy.clock import Clock, mainthread
from kivy.properties import (
    StringProperty, ListProperty, BooleanProperty,
    ObjectProperty, NumericProperty
)
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.screenmanager import Screen
from kivy.graphics import Color, RoundedRectangle

# =======================================================
#   FIX PYTHON 3.13 LOGGING + FIRESTORE CRASH
# =======================================================

# =======================================================
#   SAFE LOGGING SYSTEM (Python 3.13 • Raspberry Pi • Windows)
# =======================================================
def safe_log(level, msg, *args, **kwargs):
    """
    Prevent RecursionError in Python 3.12–3.13 by forcing all log messages
    into safe string form before sending to logging.
    """
    try:
        text = str(msg)
    except Exception:
        try:
            text = repr(msg)
        except Exception:
            text = "Unloggable message"

    logger = logging.getLogger()

    if level == "info":
        logger.info(text)
    elif level == "warning":
        logger.warning(text)
    elif level == "error":
        logger.error(text)
    elif level == "debug":
        logger.debug(text)
    else:
        logger.log(logging.INFO, text)

def wifi_keep_alive():
    """Ensure Wi-Fi stays ON on Raspberry Pi. Windows ignores this safely."""
    system = platform.system().lower()

    # Run ONLY on Raspberry Pi/Linux
    if system != "linux":
        return

    try:
        # 1. Ensure NetworkManager running

        subprocess.run(
            ["sudo","systemctl", "start", "NetworkManager"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        # 2. Wi-Fi radio ON?
        state = subprocess.getoutput("nmcli radio wifi").strip().lower()
        if state != "enabled":
            safe_log("warning","⚠️ Wi-Fi OFF detected — enabling Wi-Fi...")
            subprocess.run(["nmcli", "radio", "wifi", "on"])

        # 3. rfkill blocking Wi-Fi?
        rfkill = subprocess.getoutput("rfkill list wifi").lower()
        if "blocked: yes" in rfkill:
            safe_log("warning","⚠️ Wi-Fi rfkill block detected — unblocking...")
            subprocess.run(["rfkill", "unblock", "wifi"])

    except Exception as e:
        safe_log("error",f"Wi-Fi watchdog error: {e}")

# --------------------------
# Setup
# --------------------------

# --- Platform helpers (safe for both Kivy and stdlib) ---
def _is_linux():
    try:
        # Preferred: Kivy's platform string (e.g., 'linux', 'win', 'android', 'ios')
        from kivy.utils import platform as kv_platform
        return kv_platform in ("linux", "linux2")
    except Exception:
        # Fallback: stdlib detection
        import sys
        return sys.platform.startswith("linux")

 ######################################
 #   Returns a unique device identifier:
 #  - On Raspberry Pi: CPU serial from /proc/cpuinfo
 #  - On Windows: motherboard UUID via WMIC
 #  - Fallback: hostname-based ID
 ######################################
def get_device_id():
    """
    Returns a stable unique ID:
    - Raspberry Pi → CPU serial
    - Windows → motherboard UUID (no wmic required)
    - Others → MAC address fallback
    """
    try:
        system = platform.system().lower()

        # ───────────────────────────────
        # 1️⃣ Raspberry Pi / Linux
        # ───────────────────────────────
        if system == "linux":
            try:
                with open("/proc/cpuinfo", "r") as f:
                    for line in f:
                        if line.startswith("Serial"):
                            serial = line.split(":", 1)[1].strip()
                            if serial and serial != "0000000000000000":
                                return serial
            except:
                pass

            # Fallback for non-Pi Linux
            return platform.node()

        # ───────────────────────────────
        # 2️⃣ Windows — No more wmic
        # ───────────────────────────────
        if system == "windows":
            try:
                import uuid
                return str(uuid.getnode())  # stable MAC hardware ID
            except:
                return platform.node()

        # ───────────────────────────────
        # 3️⃣ Other OS fallback
        # ───────────────────────────────
        import uuid
        return str(uuid.uuid4())

    except Exception as e:
        safe_log("warning", f"get_device_id error: {e}")
        return "unknown_device"

try:
    import serial
except ImportError:
    serial = None

def detect_serial_port():
    if platform.system() == "Windows":
        return "COM3"
    else:
        # auto-detect first USB serial device
        ports = glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")
        return ports[0] if ports else "/dev/ttyUSB0"

# --------------------------
# Constants
# --------------------------

SERIAL_PORT = detect_serial_port()
BAUDRATE = 9600
SERIAL_CHECK_INTERVAL = 0.05   # Less CPU usage

ACCOUNT_DATA = "json_data/serviceAccountKey.json"
DATA_FILE = "json_data/account_data.json"
SETTINGS_FILE = "json_data/carwash_settings.json"
DEFAULT_SETTINGS = {
    "water_timer": 60,
    "foaming_timer": 60
}

# --------------------------
# Firebase Initialization (Safe)
# --------------------------
try:
    if not firebase_admin._apps:
        cred = credentials.Certificate(ACCOUNT_DATA)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        safe_log("info","✅ Firebase initialized successfully.")
    else:
        db = firestore.client()
except Exception as e:
    safe_log("warning",f"⚠️ Firebase initialization failed: {e}")
    db = None  # allows app to run offline


def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        save_settings(DEFAULT_SETTINGS)
        return DEFAULT_SETTINGS

    try:
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    except:
        return DEFAULT_SETTINGS

def save_settings(data):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f)
        return True
    except:
        return False

# Unique IDs (change per Raspberry Pi unit)
OWNER_ID = get_device_id() # the Firebase Auth user ID of the machine owner
MACHINE_ID = f"machine_{OWNER_ID[-6:]}"  # e.g., last 6 chars of serial
LOCATION = "Imus Branch"     # optional

# --------------------------
# Screens & UI Classes
# --------------------------
class InactivityMixin:
    inactivity_timeout = 20  # seconds

    def on_enter(self):
        self.start_inactivity_timer()
        Window.bind(on_touch_down=self._on_any_touch)

    def on_leave(self):
        self.stop_inactivity_timer()
        Window.unbind(on_touch_down=self._on_any_touch)

    def start_inactivity_timer(self):
        self.stop_inactivity_timer()
        self.inactivity_event = Clock.schedule_once(self.on_inactivity_timeout, self.inactivity_timeout)

    def stop_inactivity_timer(self):
        if hasattr(self, "inactivity_event") and self.inactivity_event:
            Clock.unschedule(self.inactivity_event)
            self.inactivity_event = None

    def _on_any_touch(self, window, touch):
        app = App.get_running_app()
        sm = app.root.ids.sm
        # Don't restart timer if we're on video screen OR if video screen is active
        if sm.current != "video" and not self._is_video_screen_active():
            self.start_inactivity_timer()

    def _is_video_screen_active(self):
        """Check if video screen is currently active in any way"""
        app = App.get_running_app()
        sm = app.root.ids.sm
        return sm.current == "video"

    def on_inactivity_timeout(self, dt):
        app = App.get_running_app()
        sm = app.root.ids.sm

        # 🔹 Skip inactivity if we're on the Wi-Fi screen
        if sm.current == "wifi":
            safe_log("info","⏸️ Inactivity ignored — Wi-Fi screen open.")
            self.start_inactivity_timer()  # restart timer for next cycle
            return

        # Don't trigger if video screen is already active
        if self._is_video_screen_active():
            safe_log("info","Inactivity timeout ignored - video already playing"),
            return

        if app.is_machine_busy():  # ✅ call app version
            safe_log("info","Inactivity ignored — machine busy (popup/coin/timer)")
            self.start_inactivity_timer()
            return

        sm = app.root.ids.sm
        app.last_screen_before_video = sm.current
        safe_log("info",f"No touch for {self.inactivity_timeout}s → switching to video from {sm.current}")
        sm.current = "video"

class VideoScreen(Screen, InactivityMixin):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._is_closing = False
        self._menu_video_was_playing = False
        self.end_check_event = None
        self._video_started = False
        self._video_already_playing = False
        self._last_video_state = None

    def on_enter(self):
        """Safe video screen entry with crash protection"""
        try:
            self._is_closing = False
            self._video_started = False
            self._video_already_playing = False

            # Check if video is already playing to avoid conflicts
            if self._is_video_already_playing:
                safe_log("warning","Video already playing - skipping video screen")
                self.emergency_navigation_fallback()
                return

            # Track menu video state before pausing
            self._track_menu_video_state()

            # Pause menu video safely
            self.safe_pause_menu_video()

            # Delay video start to ensure stability
            Clock.schedule_once(self.safe_start_video, 0.5)

        except Exception as e:
            safe_log("error",f"Video screen entry failed: {e}")
            # If entry fails, try to navigate back immediately
            Clock.schedule_once(lambda dt: self.emergency_navigation_fallback(), 1.0)

    @property
    def _is_video_already_playing(self):
        """Detect if video is already playing to prevent conflicts"""
        try:
            video = self.ids.intro_video
            if not video:
                return False

            # Check current video state
            current_state = getattr(video, 'state', 'stop')
            current_position = getattr(video, 'position', 0)
            current_duration = getattr(video, 'duration', 0)

            # Video is considered "already playing" if:
            # 1. State is 'play' and position is not at start
            # 2. State is 'play' and we're not at the very beginning
            video_playing = (
                    current_state == 'play' and
                    current_position > 1.0 and  # More than 1 second in
                    current_position < (current_duration - 1.0)  # Not near the end
            )

            if video_playing:
                safe_log("warning",
                    f"Video conflict detected: state={current_state}, pos={current_position:.1f}/{current_duration:.1f}")
                self._video_already_playing = True
                return True

            return False

        except Exception as e:
            safe_log("warning",f"Video state check failed: {e}")
            return False

    def _track_menu_video_state(self):
        """Track if menu video was playing before entering"""
        try:
            app = App.get_running_app()
            sm = app.root.ids.sm
            if sm.has_screen("menu"):
                menu_screen = sm.get_screen("menu")
                always_play_video = menu_screen.ids.always_play
                if always_play_video and hasattr(always_play_video, 'state'):
                    self._menu_video_was_playing = (always_play_video.state == "play")
                else:
                    self._menu_video_was_playing = False
        except:
            self._menu_video_was_playing = False

    def safe_pause_menu_video(self):
        """Safely pause menu background video"""
        try:
            app = App.get_running_app()
            sm = app.root.ids.sm
            if sm.has_screen("menu"):
                menu_screen = sm.get_screen("menu")
                always_play_video = menu_screen.ids.always_play
                if always_play_video and hasattr(always_play_video, 'state'):
                    always_play_video.state = "pause"
                    safe_log("info","Paused always_play video for intro")
        except Exception as e:
            safe_log("warning",f"Could not pause menu video: {e}")

    def safe_start_video(self, dt):
        """Safely start video playback with error handling"""
        if self._is_closing or self._video_already_playing:
            return  # Don't start if we're already closing or video is playing

        try:
            video = self.ids.intro_video
            if not video:
                safe_log("warning","Video widget not found")
                self.safe_auto_close_screen()
                return

            # Double-check video isn't already playing
            current_state = getattr(video, 'state', 'stop')
            if current_state == 'play':
                safe_log("warning","Video already playing - skipping start")
                self._video_already_playing = True
                self.safe_auto_close_screen()
                return

            # Reset video to beginning to ensure clean start
            try:
                if hasattr(video, 'seek'):
                    video.seek(0)
            except:
                pass

            video.state = "play"
            video.options = {"eos": "stop"}
            self._video_started = True
            self._last_video_state = 'play'

        except Exception as e:
            safe_log("error",f"Video start failed: {e}")
            # If video fails, close screen after a delay
            Clock.schedule_once(lambda dt: self.safe_auto_close_screen(), 2.0)
            return

        # Schedule a safe end check
        self.safe_schedule_end_check()
        safe_log("info","Intro video safely started")

    def safe_schedule_end_check(self):
        """Safely schedule video end monitoring"""
        try:
            # First unschedule any existing event
            self.safe_unschedule_end_check()

            # Schedule new end check
            self.end_check_event = Clock.schedule_interval(self.safe_check_video_end, 0.5)
        except Exception as e:
            safe_log("error",f"Failed to schedule end check: {e}")

    def safe_check_video_end(self, dt):
        """Safely check if video has finished playing"""
        if self._is_closing or self._video_already_playing:
            return False

        try:
            video = self.ids.intro_video
            if not video or not hasattr(video, "state"):
                safe_log("warning","Video widget unavailable in end check")
                self.safe_auto_close_screen()
                return False

            current_state = getattr(video, 'state', 'stop')
            current_position = getattr(video, 'position', 0)
            current_duration = getattr(video, 'duration', 0)

            # Track state changes for debugging
            if current_state != self._last_video_state:
                safe_log("debug",f"Video state changed: {self._last_video_state} -> {current_state}")
                self._last_video_state = current_state

            # If video reached end or stopped, close screen
            video_finished = (
                    current_state == "stop" or
                    (current_duration > 0 and current_position >= (current_duration - 0.5))
            )

            if video_finished:
                safe_log("info","Intro video finished playing - closing screen")
                self.safe_auto_close_screen()
                return False

            # Safety check: if video stopped unexpectedly but we think it should be playing
            if (current_state == "stop" and
                    self._video_started and
                    current_position < (current_duration - 5.0)):
                safe_log("warning","Video stopped unexpectedly - closing screen")
                self.safe_auto_close_screen()
                return False

        except Exception as e:
            safe_log("error",f"Video end check failed: {e}")
            self.safe_auto_close_screen()
            return False

        return True

    def on_touch_down(self, touch):
        """Safe manual skip by touching screen with crash prevention"""
        try:
            if self._is_closing or self._video_already_playing:
                return True  # Already closing or video playing, ignore touch

            safe_log("info","Touch detected - safely skipping intro video")

            # Use a small delay to ensure any ongoing video operations complete
            Clock.schedule_once(lambda dt: self.safe_auto_close_screen(), 0.1)
            return True
        except Exception as e:
            safe_log("error",f"Error in video touch handler: {e}")
            # Fallback: try to close screen anyway
            try:
                self.safe_auto_close_screen()
            except:
                pass
            return True

    def safe_auto_close_screen(self):
        """Safely close video screen with comprehensive error handling"""
        if self._is_closing:
            return  # Prevent multiple calls

        self._is_closing = True

        try:
            safe_log("info","Starting safe video screen closure")

            # Step 1: Stop video playback safely
            self.safe_cleanup_events()

            # Step 3: Resume menu video if available
            self.safe_resume_menu_video()

            # Step 4: Navigate to previous screen with delay
            Clock.schedule_once(lambda dt: self.safe_navigate_previous(), 0.2)

        except Exception as e:
            safe_log("error",f"Error in safe_auto_close_screen: {e}")
            # Emergency fallback
            self.emergency_navigation_fallback()


    def safe_cleanup_events(self):
        """Safely cleanup all scheduled events"""
        try:
            # Unschedule end check event
            self.safe_unschedule_end_check()

            # Unschedule any other potential events
            try:
                Clock.unschedule(self.safe_check_video_end)
            except:
                pass

        except Exception as e:
            safe_log("warning",f"Event cleanup failed: {e}")

    def safe_unschedule_end_check(self):
        """Safely unschedule the end check event"""
        try:
            if hasattr(self, "end_check_event") and self.end_check_event:
                Clock.unschedule(self.end_check_event)
                self.end_check_event = None
        except Exception as e:
            safe_log("warning",f"End check unschedule failed: {e}")

    def safe_resume_menu_video(self):
        """Safely resume menu background video"""
        try:
            if not self._menu_video_was_playing:
                return  # Don't resume if it wasn't playing

            app = App.get_running_app()
            sm = app.root.ids.sm
            if sm.has_screen("menu"):
                menu_screen = sm.get_screen("menu")
                always_play_video = menu_screen.ids.always_play
                if always_play_video and hasattr(always_play_video, 'state'):
                    always_play_video.state = "play"
                    safe_log("info","Menu video safely resumed")
        except Exception as e:
            safe_log("warning",f"Menu video resume failed: {e}")

    def safe_navigate_previous(self):
        """Safely navigate to previous screen"""
        try:
            app = App.get_running_app()
            sm = app.root.ids.sm

            # Get previous screen safely
            prev = getattr(app, "last_screen_before_video", "tapstart")

            # Validate screen exists before switching
            if sm.has_screen(prev):
                self._final_navigation(prev)
            else:
                safe_log("warning",f"Previous screen '{prev}' not found - using menu")
                self._final_navigation("menu")

        except Exception as e:
            safe_log("error",f"Navigation setup failed: {e}")
            self.emergency_navigation_fallback()

    def _final_navigation(self, screen_name):
        """Final navigation step after cleanup"""
        try:
            app = App.get_running_app()
            sm = app.root.ids.sm

            # Ensure we're not already on the target screen
            if sm.current != screen_name:
                sm.current = screen_name

                # Restart inactivity timer if applicable
                try:
                    current_screen = sm.get_screen(screen_name)
                    if isinstance(current_screen, InactivityMixin):
                        current_screen.start_inactivity_timer()
                except:
                    pass

            safe_log("info",f"Successfully navigated to {screen_name}")

        except Exception as e:
            safe_log("error",f"Final navigation failed: {e}")
            self.emergency_navigation_fallback()

    def emergency_navigation_fallback(self):
        """Emergency fallback navigation when all else fails"""
        try:
            app = App.get_running_app()
            sm = app.root.ids.sm

            # Try common screen names in order of priority
            fallback_screens = ["menu", "tapstart"]

            for screen_name in fallback_screens:
                if sm.has_screen(screen_name):
                    sm.current = screen_name
                    safe_log("info",f"Emergency navigation to {screen_name}")
                    return

            # Last resort - try to get first available screen
            if sm.screens:
                sm.current = sm.screens[0].name
                safe_log("info",f"Last resort navigation to {sm.screens[0].name}")

        except Exception as e:
            safe_log("error",f"All navigation attempts failed: {e}")

    def on_leave(self):
        """Safe cleanup when leaving screen"""
        try:
            self._is_closing = True
            self.safe_cleanup_events()
            safe_log("info","Video screen safely left")
        except Exception as e:
            safe_log("error",f"Video screen leave cleanup failed: {e}")

    def on_pre_leave(self):
        """Additional safety cleanup before leaving"""
        try:
            self._is_closing = True
            self.safe_unschedule_end_check()
        except Exception as e:
            safe_log("warning",f"Pre-leave cleanup failed: {e}")

class ServiceLane(BoxLayout):
    lane_name = StringProperty("")
    service_type = StringProperty("")
    lane_key = StringProperty("")

    def play_video_screen(self):
        app = App.get_running_app()
        sm = app.root
        sm.current = "video"

    def do_action(self, action):
        App.get_running_app().handle_service_request(self.lane_key, action)

    def show_popup(self):
        popup = InsertCoinPopup(lane_key=self.lane_key)
        popup.open()


# ---------- OS DETECTION ----------
def is_linux():
    return platform.system().lower() == "linux"

def is_windows():
    return platform.system().lower() == "windows"


# ---------- nmcli detection ----------
_nmcli_path = shutil.which("nmcli")

# ======================================================
#   WIFI SCAN (Windows + Raspberry Pi)
# ======================================================
def scan_wifi():
    try:
        # ---------- LINUX ----------
        if is_linux() and _nmcli_path:
            out = subprocess.check_output(
                [_nmcli_path, "-t", "-f", "SSID,SIGNAL", "device", "wifi", "list"],
                stderr=subprocess.DEVNULL
            ).decode(errors="ignore")

            networks = []
            for line in out.splitlines():
                if ":" not in line:
                    continue
                ssid, sig = line.split(":", 1)
                ssid = ssid.strip()
                if ssid:
                    networks.append({"ssid": ssid, "signal": sig})
            return networks

        # ---------- WINDOWS ----------
        if is_windows():
            out = subprocess.check_output(
                ["netsh", "wlan", "show", "networks", "mode=Bssid"],
                shell=True, stderr=subprocess.DEVNULL
            ).decode(errors="ignore")

            networks, ssid = [], None
            for line in out.splitlines():
                L = line.strip()

                if L.lower().startswith("ssid"):
                    ssid = L.split(":", 1)[1].strip()

                elif "signal" in L.lower() and ssid:
                    signal = L.split(":", 1)[1].replace("%", "").strip()
                    networks.append({"ssid": ssid, "signal": signal})
                    ssid = None

            return networks

        return []

    except Exception as e:
        return []

# ======================================================
#   CONNECT WIFI
# ======================================================
def connect_wifi(ssid, password):
    try:
        # ---------- WINDOWS ----------
        if is_windows():
            subprocess.run(
                f'netsh wlan delete profile name="{ssid}"',
                shell=True, stdout=subprocess.DEVNULL
            )

            xml = f"""
            <WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
                <name>{ssid}</name>
                <SSIDConfig><SSID><name>{ssid}</name></SSID></SSIDConfig>
                <connectionType>ESS</connectionType>
                <MSM><security>
                    <authEncryption>
                        <authentication>WPA2PSK</authentication>
                        <encryption>AES</encryption>
                        <useOneX>false</useOneX>
                    </authEncryption>
                    <sharedKey>
                        <keyType>passPhrase</keyType>
                        <protected>false</protected>
                        <keyMaterial>{password}</keyMaterial>
                    </sharedKey>
                </security></MSM>
            </WLANProfile>
            """

            temp = os.path.join(tempfile.gettempdir(), f"{ssid}.xml")
            with open(temp, "w") as f:
                f.write(xml)

            subprocess.run(
                f'netsh wlan add profile filename="{temp}"',
                shell=True, stdout=subprocess.DEVNULL
            )
            result = subprocess.run(
                f'netsh wlan connect name="{ssid}"',
                shell=True, stdout=subprocess.PIPE
            )

            if "success" in result.stdout.decode().lower():
                return True, "Connected"
            return False, "Wrong password"

        # ---------- LINUX (Raspberry Pi) ----------
        if is_linux() and _nmcli_path:

            # Delete old stored connection (if exists)
            subprocess.run(
                [_nmcli_path, "connection", "delete", ssid],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )

            # ✔ CORRECT NMCLI USAGE
            result = subprocess.run(
                [
                    "nmcli", "device", "wifi", "connect", ssid,
                    "password", password
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            if result.returncode == 0:
                return True, "Connected"

            if "wrong" in result.stderr.lower():
                return False, "Wrong password"

            return False, result.stderr.strip()

        return False, "Wi-Fi not supported"

    except Exception as e:
        return False, str(e)

# ======================================================
#   FORGET NETWORK
# ======================================================
def forget_wifi(ssid):
    system = platform.system().lower()

    try:
        # ---------- WINDOWS ----------
        if system == "windows":
            subprocess.run(
                f'netsh wlan delete profile name="{ssid}"',
                shell=True, stdout=subprocess.DEVNULL
            )
            return True, f"Forgot {ssid}"

        # ---------- LINUX ----------
        if system == "linux" and _nmcli_path:
            subprocess.run(
                [_nmcli_path, "connection", "delete", ssid],
                stderr=subprocess.DEVNULL
            )
            return True, f"Forgot {ssid}"

        return False, "Unsupported OS"

    except Exception as e:
        return False, str(e)

# ======================================================
#   GET CURRENT SSID
# ======================================================
def get_current_ssid():
    try:
        if is_windows():
            out = subprocess.check_output(
                ["netsh", "wlan", "show", "interfaces"],
                shell=True
            ).decode(errors="ignore")
            for line in out.splitlines():
                if "SSID" in line and "BSSID" not in line:
                    return line.split(":", 1)[1].strip()

        if is_linux() and _nmcli_path:
            out = subprocess.check_output(
                [_nmcli_path, "-t", "-f", "ACTIVE,SSID", "device", "wifi"]
            ).decode()
            for line in out.splitlines():
                if line.startswith("yes:"):
                    return line.split(":")[1]

        return "Not connected"
    except:
        return "Not connected"

# ======================================================
#   TOGGLE WIFI (ON/OFF)
# ======================================================
def wifi_is_on():
    if is_windows():
        out = subprocess.check_output(
            ["netsh", "interface", "show", "interface", "Wi-Fi"],
            shell=True
        ).decode().lower()
        return "enabled" in out

    if is_linux():
        try:
            out = subprocess.check_output(
                ["rfkill", "list", "wifi"]
            ).decode().lower()
            return "soft blocked: yes" not in out
        except:
            return True

    return False


def toggle_wifi():
    try:
        if wifi_is_on():
            subprocess.run(["sudo", "rfkill", "block", "wifi"])
            return True, "Wi-Fi OFF"
        else:
            subprocess.run(["sudo", "rfkill", "unblock", "wifi"])
            return True, "Wi-Fi ON"
    except Exception as e:
        return False, str(e)

class WifiPasswordPopup(Popup):
    ssid = StringProperty("")

    def connect(self, *_):
        password = self.ids.password_input.text.strip()
        if not password:
            self.ids.status_label.text = "Enter password"
            return

        self.ids.status_label.text = "Connecting..."
        threading.Thread(target=self._thread, args=(password,), daemon=True).start()

    def _thread(self, pwd):
        ok, msg = connect_wifi(self.ssid, pwd)
        Clock.schedule_once(lambda dt: self._done(ok, msg))

    @mainthread
    def _done(self, ok, msg):
        self.ids.status_label.text = msg
        if ok:
            Clock.schedule_once(lambda dt: self.dismiss(), 1.2)

class WifiScreen(Screen):

    wifi_status_text = StringProperty("[b]Wi-Fi: Checking...[/b]")
    wifi_on = BooleanProperty(False)

    scanning = False
    scan_event = None

    # --------------------------------------------
    def on_pre_enter(self):
        self.update_wifi_status()
        self.update_current_network()
        self.start_realtime_scan()

        Clock.schedule_interval(lambda dt: self.update_wifi_status(), 3)
        Clock.schedule_interval(lambda dt: self.update_current_network(), 4)

    # --------------------------------------------
    def update_wifi_status(self):
        state = wifi_is_on()
        self.wifi_on = state
        self.wifi_status_text = "[b]Wi-Fi: ON[/b]" if state else "[b]Wi-Fi: OFF[/b]"

    # --------------------------------------------
    def toggle_wifi_button(self):
        ok, msg = toggle_wifi()
        self.update_wifi_status()
        self.update_current_network()
        self._popup(msg)

    # --------------------------------------------
    def update_current_network(self):
        ssid = get_current_ssid()
        color = (0,1,0,1) if ssid != "Not connected" else (1,0.3,0.3,1)
        self.ids.current_network_label.text = f"[b]Connected:[/b] {ssid}"
        self.ids.current_network_label.color = color

    # --------------------------------------------
    def start_realtime_scan(self):
        if not self.scan_event:
            self.scan_event = Clock.schedule_interval(self._perform_scan, 1.5)

    def _perform_scan(self, dt):
        if self.scanning:
            return
        self.scanning = True
        threading.Thread(target=self._scan_thread, daemon=True).start()

    def _scan_thread(self):
        networks = scan_wifi()
        Clock.schedule_once(lambda dt: self._update_scan(networks))
        self.scanning = False

    @mainthread
    def _update_scan(self, networks):
        if not networks:
            self.ids.rv.data = [{"text": "[b]No networks found[/b]"}]
            return

        self.ids.rv.data = [
            {
                "text": f"[b]{n['ssid']}[/b] - {n['signal']}%",
                "ssid": n["ssid"],
                "on_release": lambda ssid=n["ssid"]: self.open_password(ssid)
            }
            for n in networks
        ]

    # --------------------------------------------
    def open_password(self, ssid):
        WifiPasswordPopup(ssid=ssid).open()

    # --------------------------------------------
    def forget_network(self):
        ssid = get_current_ssid()
        if ssid in ("Not connected", ""):
            self._popup("Nothing to forget")
            return

        ok, msg = forget_wifi(ssid)
        self.update_current_network()
        self._popup(msg)

    # --------------------------------------------
    def _popup(self, msg):
        popup = Popup(
            title="",
            size_hint=(0.5,0.25),
            auto_dismiss=True
        )
        layout = BoxLayout(orientation="vertical", padding=15)
        layout.add_widget(Label(text=msg, halign="center"))
        btn = Button(text="OK", size_hint=(1,0.3), on_release=popup.dismiss)
        layout.add_widget(btn)
        popup.content = layout
        popup.open()

    def exit_wifi(self):
        """Return to menu screen safely."""
        app = App.get_running_app()
        try:
            sm = app.root.ids.sm
            if sm.has_screen("tapstart"):
                sm.current = "tapstart"
            else:
                sm.current = "menu"
        except Exception as e:
            print("Exit Wi-Fi error:", e)

class SettingListScreen(Screen):
    pass

class TimerSettingsScreen(Screen):

    def on_pre_enter(self):
        app = App.get_running_app()

        # Load current settings into temporary variables
        self.temp_water = app.settings.get("water_timer", 60)
        self.temp_foam = app.settings.get("foaming_timer", 60)

        # Update UI
        self.ids.water_timer_value.text = f"{self.temp_water}s"
        self.ids.foaming_timer_value.text = f"{self.temp_foam}s"

    def adjust_timer(self, lane_key, delta):
        if lane_key == "L":
            self.temp_water = max(10, min(300, self.temp_water + delta))
            self.ids.water_timer_value.text = f"{self.temp_water}s"

        else:
            self.temp_foam = max(10, min(300, self.temp_foam + delta))
            self.ids.foaming_timer_value.text = f"{self.temp_foam}s"

    def save_settings(self):
        """Save temp values to JSON + update app.settings"""
        app = App.get_running_app()

        # Update app settings
        app.settings["water_timer"] = self.temp_water
        app.settings["foaming_timer"] = self.temp_foam

        # Save JSON
        save_settings(app.settings)

        safe_log("info", f"Timer settings saved: Water={self.temp_water}s, Foaming={self.temp_foam}s")

        # OPTIONAL: Confirm popup
        popup = Popup(
            title="Saved",
            content=Label(text="Timer settings updated successfully!"),
            size_hint=(0.4, 0.25),
        )
        popup.open()

        # Return to settings main page
        Clock.schedule_once(lambda dt: setattr(app.root.ids.sm, "current", "timer_settings"), 0.5)

class TapToStartScreen(Screen, InactivityMixin):
    pass

class ArduinoDisconnectedPopup(Popup):
    """Popup shown when Arduino is not connected in the MenuScreen."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.title = ""
        self.size_hint = (0.65, 0.45)
        self.auto_dismiss = False  # Prevent closing unless reconnected

    def on_open(self):
        """Optional alert sound or LED blink"""
        app = App.get_running_app()
        app.send_serial_command("BEEP_ON")
        Clock.schedule_once(lambda dt: app.send_serial_command("BEEP_OFF"), 0.3)

class MenuScreen(Screen,InactivityMixin):
    def refresh_menu_after_timer_change(self):
        app = App.get_running_app()
        # No direct timer labels require update now, but this prevents stale UI bugs
        safe_log("info", "Menu refreshed after timer update")

    def on_enter(self):
        # Start the always_play video when entering menu
        Clock.schedule_once(self.start_always_play_video, 0.5)
        Clock.schedule_once(lambda dt: self.check_arduino_status(), 0.5)

    def start_always_play_video(self, dt):
        """Start the looping background video"""
        try:
            always_play_video = self.ids.always_play
            if always_play_video and hasattr(always_play_video, 'state'):
                # Check if any timer is running to determine which video to play
                app = App.get_running_app()
                if app.left_lane.running or app.right_lane.running:
                    always_play_video.source = "washing_video.mp4"  # Video when timer running
                else:
                    always_play_video.source = "always_play.mp4"  # Default video

                always_play_video.state = "play"
                always_play_video.options = {"eos": "loop"}
                safe_log("info","Always_play video started in menu")
        except Exception as e:
            safe_log("warning",f"Could not start always_play video: {e}")

    def switch_to_washing_video(self):
        """Switch to washing video when timer starts"""
        try:
            always_play_video = self.ids.always_play
            if always_play_video and hasattr(always_play_video, 'state'):
                # Only switch if not already playing washing video
                if always_play_video.source != "washing_video.mp4":
                    always_play_video.source = "washing_video.mp4"
                    always_play_video.state = "play"
                    always_play_video.options = {"eos": "loop"}
                    safe_log("info","Switched to washing video")
        except Exception as e:
            safe_log("warning",f"Could not switch to washing video: {e}")

    def switch_to_default_video(self):
        """Switch back to default video when all timers stop"""
        try:
            always_play_video = self.ids.always_play
            if always_play_video and hasattr(always_play_video, 'state'):
                # Only switch if not already playing default video
                if always_play_video.source != "always_play.mp4":
                    always_play_video.source = "always_play.mp4"
                    always_play_video.state = "play"
                    always_play_video.options = {"eos": "loop"}
                    safe_log("info","Switched back to default video")
        except Exception as e:
            safe_log("warning",f"Could not switch to default video: {e}")

    def on_leave(self):
        """Pause video when leaving menu screen"""
        try:
            always_play_video = self.ids.always_play
            if always_play_video and hasattr(always_play_video, 'state'):
                always_play_video.state = "pause"
                safe_log("info","Always_play video paused")
        except Exception as e:
            safe_log("warning",f"Could not pause always_play video: {e}")

    def check_arduino_status(self):
        app = App.get_running_app()

        # ✅ Try to reconnect once before showing popup
        app.connect_serial()

        if not app.serial_port or not getattr(app.serial_port, "is_open", False):
            self.show_arduino_popup()

    def show_arduino_popup(self):
        popup = ArduinoDisconnectedPopup()
        popup.open()
        safe_log("info","⚠️ Arduino not connected — popup shown.")

        app = App.get_running_app()
        app.send_serial_command("BEEP_ON")
        Clock.schedule_once(lambda dt: app.send_serial_command("BEEP_OFF"), 0.3)

        # Auto close once
        def auto_close(dt):
            if app.serial_port and getattr(app.serial_port, "is_open", False):
                popup.dismiss()
                safe_log("info","✅ Arduino reconnected — popup closed automatically.")
                return False
            return True

        Clock.schedule_interval(auto_close, 2)

# Insert Coin Popup
class InsertCoinPopup(Popup):
    content_box = ObjectProperty(None)  # reference to BoxLayout in KV

    def __init__(self, lane_key="L", **kwargs):
        super().__init__(**kwargs)
        self.lane_key = lane_key
        self.countdown = 15
        self.countdown_event = None
        self.coin_inserted = False
        self.beep_event = None

    def on_open(self):
        # ✅ Animate the inner container (not the popup)
        if self.content_box:
            self.content_box.opacity = 0
            self.content_box.scale = 0.9
            anim = Animation(opacity=1, scale=1.0, d=0.25, t='out_back')
            anim.start(self.content_box)

        app = App.get_running_app()
        app.active_popup = self

        # Identify lane
        lane_name = "Water" if self.lane_key == "L" else "Foaming"
        lane = app.left_lane if self.lane_key == "L" else app.right_lane

        # ✅ Update labels immediately
        if "lane_label" in self.ids:
            self.ids.lane_label.text = f"Lane: {lane_name}"
            self.ids.lane_label.texture_update()

        if "coin_label" in self.ids:
            # Refresh to show the current total coins when popup opens
            self.ids.coin_label.text = f"Credit's: {lane.coins}"
            self.ids.coin_label.texture_update()

            # ✅ Extra step (for Raspberry Pi)
            # Schedule a redraw on the next frame so the Pi GPU catches the update
            from kivy.base import EventLoop
            Clock.schedule_once(lambda dt: EventLoop.idle(), 0)
            Clock.schedule_once(lambda dt: self.ids.coin_label.canvas.ask_update(), 0.05)

        # ✅ Restart countdown timer on every open
        self.start_countdown()

        safe_log("info",
            f"Popup opened for lane {self.lane_key} ({lane_name}), showing {lane.coins} coins, waiting for more..."
        )

    def dismiss(self, *args, **kwargs):
        # ✅ Animate the inner content before closing
        if self.content_box:
            anim = Animation(opacity=0, scale=0.9, d=0.2, t='in_quad')
            anim.bind(on_complete=lambda *x: super(InsertCoinPopup, self).dismiss(*args, **kwargs))
            anim.start(self.content_box)
        else:
            super().dismiss(*args, **kwargs)



    def start_countdown(self):
        if self.countdown_event:
            Clock.unschedule(self.countdown_event)
        self.countdown = 15
        self.ids.countdown_label.text = f"{self.countdown}s"
        self.ids.countdown_label.color = [0.6, 0.9, 1, 1]
        self.countdown_event = Clock.schedule_interval(self._update_countdown, 1)

    @mainthread
    def on_coin_inserted(self):
        """Triggered by serial message; updates popup label and restarts timer safely on Pi."""
        app = App.get_running_app()
        lane = app.left_lane if self.lane_key == "L" else app.right_lane

        # --- Update label directly ---
        if "coin_label" in self.ids:
            self.ids.coin_label.text = f"Credit's: {lane.coins}"
            self.ids.coin_label.texture_update()

            # ✅ Force redraw (needed for Raspberry Pi GPU)
            from kivy.base import EventLoop
            Clock.schedule_once(lambda dt: EventLoop.idle(), 0.02)
            Clock.schedule_once(lambda dt: self.ids.coin_label.canvas.ask_update(), 0.05)

        # --- Restart countdown timer safely ---
        if hasattr(self, "countdown_event") and self.countdown_event:
            Clock.unschedule(self.countdown_event)
        self.start_countdown()

        # --- Pulse animation feedback ---
        self.animate_label_color([0.0, 0.9, 0.9, 1])
        anim = Animation(font_size=45, d=0.15) + Animation(font_size=40, d=0.15)
        anim.start(self.ids.coin_label)

        safe_log("info",f"Popup updated live with {lane.coins} credits")

    def _update_countdown(self, dt):
        self.countdown -= 1
        self.ids.countdown_label.text = f"{self.countdown}s"

        if self.countdown > 3:
            self.animate_label_color([0.0, 0.8, 1.0, 1])
        elif self.countdown == 3:
            self.animate_label_color([1.0, 0.6, 0.0, 1])
        elif self.countdown <= 2:
            self.animate_label_color([1.0, 0.2, 0.2, 1])

        if self.countdown <= 0:
            safe_log("info",f"Popup timeout lane {self.lane_key}")
            self.dismiss()
            return False
        return True

    def animate_label_color(self, color):
        anim = Animation(color=color, d=0.5, t="out_quad")
        anim.start(self.ids.countdown_label)

    def on_dismiss(self):
        app = App.get_running_app()
        if getattr(app, "refreshing_popup", False):
            safe_log("info",f"Popup {self.lane_key} closed for refresh — keeping relay ON.")
            return

        if getattr(app, "active_popup", None) == self:
            app.active_popup = None

        app.send_serial_command("DISABLE_COIN")
        safe_log("info",f"Popup closed lane {self.lane_key} → coin input disabled.")

class ConfirmStopPopup(Popup):
    def __init__(self, lane_key, **kwargs):
        super().__init__(**kwargs)
        self.lane_key = lane_key

    def confirm(self):
        app = App.get_running_app()
        self.dismiss()
        app.stop_lane(self.lane_key)  # ✅ Will clear time and coins

    def cancel(self):
        self.dismiss()

# -----------------------------------------------------
# HoverBehavior — safe for both Desktop & Raspberry Pi
# -----------------------------------------------------
class HoverBehavior:
    hovered = BooleanProperty(False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # ✅ Only bind mouse motion if the Window object supports it
        supports_mouse = getattr(Window, "supports_mouse_cursor", False)
        if supports_mouse:
            try:
                Window.bind(mouse_pos=self.on_mouse_pos)
            except Exception as e:
                # Fail silently if SDL Window doesn't support mouse_pos binding
                safe_log("warning",f"HoverBehavior: mouse binding not supported ({e})")

    def on_mouse_pos(self, *args):
        """Called when the mouse moves — only active on desktop."""
        if not self.get_root_window():
            return
        pos = args[1]
        inside = self.collide_point(*self.to_widget(*pos))
        if self.hovered == inside:
            return
        self.hovered = inside
        if inside:
            self.on_hover_enter()
        else:
            self.on_hover_leave()

    def on_hover_enter(self):  # to be overridden
        pass

    def on_hover_leave(self):  # to be overridden
        pass

# -----------------------------------------------------
# HoverButton — Desktop hover + Touch (on_press) pulse
# -----------------------------------------------------
class HoverButton(Button, HoverBehavior):
    """
    Smart HoverButton:
      - Desktop: real hover color animation.
      - Touchscreen (Raspberry Pi): instant animation on touch down.
    """
    bg_color_insert = ListProperty([0.012, 0.188, 0.412, 1.0])
    bg_color_start  = ListProperty([0.0, 0.592, 0.698, 1.0])
    bg_color_home   = ListProperty([1, 1, 1, 1])
    color_instruction = ObjectProperty(None)
    scale = NumericProperty(1.0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # ✅ Detect if running on touchscreen (no mouse support)
        self._is_touch_env = not getattr(Window, "supports_mouse_cursor", False)

    # -------------------------------------------------
    # Desktop hover effects
    # -------------------------------------------------
    def on_hover_enter(self):
        if self._is_touch_env:
            return
        Animation(
            bg_color_insert=[0.0, 0.8, 0.9, 1.0],
            bg_color_start=[0.0, 0.8, 0.9, 1.0],
            bg_color_home=[0.0, 0.8, 0.9, 1.0],
            d=0.15
        ).start(self)

    def on_hover_leave(self):
        if self._is_touch_env:
            return
        Animation(
            bg_color_insert=[0.012, 0.188, 0.412, 1.0],
            bg_color_start=[0.0, 0.592, 0.698, 1.0],
            bg_color_home=[1, 1, 1, 1],
            d=0.15
        ).start(self)

    # -------------------------------------------------
    # Touchscreen feedback (on_touch_down)
    # -------------------------------------------------
    def on_touch_down(self, touch):
        # Only animate if the touch is inside this button
        if self.collide_point(*touch.pos) and self._is_touch_env:
            color_anim = (
                Animation(
                    bg_color_insert=[0.0, 0.8, 0.9, 1.0],
                    bg_color_start=[0.0, 0.8, 0.9, 1.0],
                    bg_color_home=[0.0, 0.8, 0.9, 1.0],
                    d=0.1, t="out_quad"
                )
                + Animation(
                    bg_color_insert=[0.012, 0.188, 0.412, 1.0],
                    bg_color_start=[0.0, 0.592, 0.698, 1.0],
                    bg_color_home=[1, 1, 1, 1],
                    d=0.25, t="out_quad"
                )
            )

            scale_anim = (
                Animation(scale=1.07, d=0.08, t="out_back") +
                Animation(scale=1.0, d=0.18, t="in_quad")
            )

            color_anim.start(self)
            scale_anim.start(self)

        # Always pass to the original touch handler
        return super().on_touch_down(touch)

class HoverBoxLayout(BoxLayout, HoverBehavior):
    show_shadow = BooleanProperty(True)

    def on_hover_enter(self):
        self.show_shadow = 0

    def on_hover_leave(self):
        self.show_shadow = 1


class MainRoot(BoxLayout):
    pass

# --------------------------
# Lane State
# --------------------------
class LaneState:
    def __init__(self, lane_key):
        self.lane_key = lane_key
        self.remaining = 0
        self.running = False
        self.coins = 0
        self.pending_coin = False
        self.wait_start = None
        self.pending_insert = 10

    def add_time(self, secs):
        self.remaining += int(secs)

    def tick(self):
        if self.running and self.remaining > 0:
            self.remaining -= 1
            if self.remaining <= 0:
                self.remaining = 0
                self.running = False
                return True
        return False

    def start_wait_for_coin(self):
        self.pending_coin = True
        self.wait_start = time.time()

    def check_wait_timeout(self):
        if self.wait_start is None:
            self.wait_start = time.time()
        if self.pending_coin and self.wait_start:
            if time.time() - self.wait_start > self.pending_insert:
                self.pending_coin = False
                self.wait_start = None
                return True
        return False


# --------------------------
# Main App
# --------------------------
class CarwashApp(App):
    def build(self):
        self.settings = load_settings()
        # ✅ Always defined — prevents crashes and allows rechecking anytime
        self.serial_port = None
        self.serial_alive = False
        self.simulation = False
        self.refreshing_popup = False
        self.title = "Carwash Vendo Machine"
        self.left_lane = LaneState("L")
        self.right_lane = LaneState("R")

        self.root = MainRoot()
        sm = self.root.ids.sm
        sm.transition = FadeTransition(duration=0.4)
        self.connect_serial()

        # ✅ Automatically check Arduino connection every 3 seconds
        Clock.schedule_interval(self.check_serial_connection, 3)
        Clock.schedule_interval(self.update_timers, 1.0)

        # Track previous running state to detect changes
        self.previous_left_running = False
        self.previous_right_running = False

        # Track beep states to avoid repeated beeping
        self.left_lane_beeped = False
        self.right_lane_beeped = False

        # Countdown beeping properties
        self.left_countdown_event = None
        self.right_countdown_event = None
        self.left_countdown_seconds = 0
        self.right_countdown_seconds = 0

        return self.root

    def get_timer_for_lane(self, lane_key):
        if lane_key == "L":
            return self.settings.get("water_timer", 60)
        else:
            return self.settings.get("foaming_timer", 60)

    def update_timer_setting(self, lane_key, seconds):
        if lane_key == "L":
            self.settings["water_timer"] = seconds
        else:
            self.settings["foaming_timer"] = seconds

        save_settings(self.settings)

    def on_start(self):
        Clock.schedule_interval(lambda dt: wifi_keep_alive(), 10)

        # Delay slightly so UI loads first
        Clock.schedule_once(lambda dt: self.check_machine_authorized(), 1)
        ok = self.check_machine_authorized()

        if not ok:
            safe_log("error","Machine unauthorized — system UI frozen.")
            return

        # Normal startup
        self.background_sync_to_firebase()
        self.start_realtime_sync()
        threading.Thread(target=self.listen_for_commands, daemon=True).start()

    def check_machine_authorized(self):
        """Check if MACHINE_ID exists in Firestore authorized_machines.
           Supports offline mode using locally stored is_authorized flag.
        """
        data = {}

        # Load local data (if exists)
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r") as f:
                    data = json.load(f)
            except json.JSONDecodeError:
                data = {}

        # Default value if missing
        is_local_auth = data.get("is_authorized", False)

        try:
            # ───────────────────────────────────────────────
            # 1️⃣ OFFLINE MODE — NO INTERNET
            # ───────────────────────────────────────────────
            if not self.is_connected():
                safe_log("info","No internet — offline authorization fallback.")

                if not is_local_auth:
                    Clock.schedule_once(lambda dt: self.show_unauthorized_popup(), 0.3)
                    return False

                return True  # Allowed because previously authorized offline

            # ───────────────────────────────────────────────
            # 2️⃣ FIRESTORE NOT INITIALIZED
            # ───────────────────────────────────────────────
            if db is None:
                safe_log("warning","Firestore not initialized — skipping authorization.")
                return True  # allow machine to run but do NOT update local auth

            # ───────────────────────────────────────────────
            # 3️⃣ ONLINE CHECK — FIRESTORE LOOKUP
            # ───────────────────────────────────────────────
            doc = db.collection("authorized_machines").document(MACHINE_ID).get()

            if not doc.exists:
                safe_log("error",f"❌ MACHINE_ID '{MACHINE_ID}' NOT FOUND in authorized_machines!")

                # Save local offline block state
                data["is_authorized"] = False
                with open(DATA_FILE, "w") as f:
                    json.dump(data, f, indent=4)

                Clock.schedule_once(lambda dt: self.show_unauthorized_popup(), 0.3)
                return False

            # ───────────────────────────────────────────────
            # 4️⃣ AUTHORIZED — Save offline approval
            # ───────────────────────────────────────────────
            data["is_authorized"] = True
            with open(DATA_FILE, "w") as f:
                json.dump(data, f, indent=4)

            safe_log("info",f"✅ MACHINE_ID '{MACHINE_ID}' is authorized.")
            return True

        except Exception as e:
            safe_log("error",f"Authorization check error: {e}")
            return True  # allow app during unknown error

    @mainthread
    def show_unauthorized_popup(self):
        # Main popup container
        layout = BoxLayout(
            orientation="vertical",
            padding=25,
            spacing=18
        )

        # Background styling
        with layout.canvas.before:
            from kivy.graphics import Color, RoundedRectangle
            Color(0.15, 0.15, 0.15, 0.92)  # dark overlay
            layout.bg_rect = RoundedRectangle(radius=[20])

        def update_bg(*args):
            layout.bg_rect.pos = layout.pos
            layout.bg_rect.size = layout.size

        layout.bind(pos=update_bg, size=update_bg)

        # Title Label
        title = Label(
            text="⚠️ [b]UNAUTHORIZED MACHINE[/b]",
            markup=True,
            font_size="30sp",
            color=(1, 0.3, 0.3, 1),
            font_name="assets/big-shoulders-display.bold",
            size_hint_y=None,
            height=40,
        )

        # Message Label
        message = Label(
            text=(
                "[b]Access Denied[/b]\n\n"
                "This device is NOT registered in our system.\n"
                "Please REMOVE this software immediately.\n\n"
                "[color=#ff6666]Unauthorized use is ILLEGAL.[/color]"
            ),
            markup=True,
            halign="center",
            valign="middle",
            font_size="20sp",
            color=(1, 1, 1, 1),
            font_name="assets/ITC-THIN"
        )

        # Ensures proper text alignment
        message.bind(size=lambda *a: setattr(message, "text_size", message.size))

        # Action button (shutdown)
        btn = Button(
            text="[b]RESTART[/b]",
            markup=True,
            size_hint=(1, 0.22),
            background_normal="",
            background_color=(0.8, 0.1, 0.1, 1),
            font_size="22sp",
            font_name="assets/big-shoulders-display.bold",
            color=(1, 1, 1, 1)
        )

        # Shut down the device
        def shutdown(*_):
            import subprocess
            subprocess.Popen(["sudo", "reboot", "now"])

        btn.bind(on_release=shutdown)

        # Add widgets
        layout.add_widget(title)
        layout.add_widget(message)
        layout.add_widget(btn)

        popup = Popup(
            title="",
            content=layout,
            size_hint=(0.85, 0.55),
            auto_dismiss=False,
            separator_height=0,
            background_color=(0, 0, 0, 0),
        )

        popup.open()
        self.unauthorized_popup = popup

    @mainthread
    def update_popup_coin(self, popup):
        """Run popup update in main UI thread."""
        try:
            if popup and hasattr(popup, 'on_coin_inserted'):
                popup.on_coin_inserted()
        except Exception as e:
            safe_log("warning",f"update_popup_coin error: {e}")

    def start_auto_carousel(self, dt):
        try:
            sm = self.root.ids.sm
            menu = sm.get_screen("menu")
            carousel = menu.ids.car_frame_carousel
            Clock.schedule_interval(lambda _: carousel.load_next(), 4)
        except Exception as e:
            safe_log("warning",f"Carousel start failed: {e}")

    def is_machine_busy(self):
        """Return True if any popup is open, lane running, or credit exists."""
        popup_open = any(isinstance(w, Popup) and w._window for w in Window.children)

        left_active = bool(self.left_lane.running)
        right_active = bool(self.right_lane.running)

        #  NEW: credit check
        left_credit = self.left_lane.coins > 0
        right_credit = self.right_lane.coins > 0

        # Logging for clarity
        safe_log("info",
            f"[BUSY CHECK] Popup={popup_open}, "
            f"Left(run={self.left_lane.running}, credit={self.left_lane.coins}), "
            f"Right(run={self.right_lane.running}, credit={self.right_lane.coins})"
        )

        return popup_open or left_active or right_active or left_credit or right_credit

    def process_serial_message(self, message):
        """Handle serial messages from Arduino — supports live popup update."""
        if not message or message.startswith("ACK"):
            return

        parts = message.split(":")
        if parts[0] == "COIN":
            try:
                coin_value = int(parts[1]) if len(parts) > 1 else 5
            except ValueError:
                coin_value = 5

            target = getattr(self, "last_interacted_lane", "L")
            lane = self.left_lane if target == "L" else self.right_lane

            # Only accept coins if popup is waiting
            if lane.pending_coin:

                # Determine seconds per ₱5 based on lane settings
                seconds_per_coin = self.get_timer_for_lane(target)

                # Update lane credit & time
                lane.coins += coin_value
                time_added = seconds_per_coin * (coin_value // 5)
                lane.add_time(time_added)

                safe_log("info",
                         f"💰 Coin inserted lane {target} +₱{coin_value} / +{time_added}s "
                         f"(rate={seconds_per_coin}s per ₱5)"
                         )

                lane.wait_start = None
                self.save_account_data(target, coin_value)

                # Update popup if open
                app = App.get_running_app()
                popup = getattr(app, "active_popup", None)
                app.refreshing_popup = True

                if popup and isinstance(popup, InsertCoinPopup) and popup.lane_key == target:
                    Clock.schedule_once(lambda dt: self.update_popup_coin(popup), 0)

                Clock.schedule_once(lambda dt: setattr(app, "refreshing_popup", False), 0.3)

                # Debounce to avoid double count
                Clock.schedule_once(lambda dt: setattr(lane, "pending_coin", True), 0.5)

            else:
                safe_log("info", f"⚠️ Coin ignored — lane {target} not waiting for coin")

    # --------------------------
    # Serial
    # --------------------------
    def connect_serial(self, *args):
        """Try to connect to Arduino."""
        if serial is None:
            self.simulation = True
            safe_log("warning","pyserial not installed — SIMULATION mode.")
            return

        try:
            # ✅ If already open, don't reconnect
            if self.serial_port and getattr(self.serial_port, "is_open", False):
                safe_log("info","🔌 Arduino already connected — skipping reconnect.")
                return

            self.serial_port = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=0.1)
            self.serial_alive = True
            self.simulation = False
            self.serial_thread = threading.Thread(target=self.serial_listener, daemon=True)
            self.serial_thread.start()

            safe_log("info",f"✅ Arduino connected on {SERIAL_PORT}")
        except Exception as e:
            safe_log("warning",f"❌ Arduino connection failed: {e}")
            self.serial_port = None
            self.simulation = True

    def check_serial_connection(self, dt):
        """Continuously check Arduino status and reconnect if lost."""
        try:
            # If no port or not open → try reconnect
            if not self.serial_port or not getattr(self.serial_port, "is_open", False):
                if not self.simulation:
                    safe_log("warning","⚠️ Arduino not detected — attempting reconnect...")
                    self.connect_serial()
            else:
                # ✅ If alive, mark as active (restores popup auto-close)
                if not self.serial_alive:
                    self.serial_alive = True
                    safe_log("info","✅ Arduino connection restored.")
        except Exception as e:
            safe_log("warning",f"Serial check failed: {e}")

    def serial_listener(self):
        while getattr(self, "serial_alive", False):
            try:
                if self.serial_port and getattr(self.serial_port, "in_waiting", 0):
                    line = self.serial_port.readline().decode(errors='ignore').strip()
                    if line:
                        self.process_serial_message(line)
                else:
                    time.sleep(SERIAL_CHECK_INTERVAL)
            except Exception as e:
                safe_log("warning",f"Serial read error: {e}")
                time.sleep(0.5)

    def send_serial_command(self, cmd: str):
        threading.Thread(target=self._safe_send_serial, args=(cmd,), daemon=True).start()

    def _safe_send_serial(self, cmd):
        if getattr(self, "simulation", False):
            safe_log("info",f"[SIM] TX: {cmd}")
            return
        try:
            if self.serial_port and getattr(self.serial_port, "is_open", False):
                self.serial_port.write((cmd + "\n").encode())
        except Exception as e:
            safe_log("warning",f"Serial write error: {e}")

    # --------------------------
    # UI Button handler
    # --------------------------
    def handle_service_request(self, lane_key, action):
        """Respond to INSERT_COIN / START button presses."""
        lane = self.left_lane if lane_key == "L" else self.right_lane
        self.last_interacted_lane = lane_key
        # ✅ Track lane for Arduino COIN messages
        App.get_running_app().last_interacted_lane = lane_key

        if action == "INSERT_COIN":
            lane.start_wait_for_coin()
            self.send_serial_command("ENABLE_COIN")  # ✅ allow physical coin entry
            safe_log("info",f"Insert Coin pressed for lane {lane_key} → waiting for coin...")


        elif action == "START":
            # start the lane if coins are available
            self.start_lane_timer(lane_key)

    # --------------------------
    # Manual Start Button
    # --------------------------

    def stop_lane(self, lane_key):
        """Stop the specified lane immediately: stop relay, reset timer, and clear coins."""
        lane = self.left_lane if lane_key == "L" else self.right_lane

        if lane.running or lane.remaining > 0 or lane.coins > 0:
            self.send_serial_command(f"RELAY_OFF:{lane_key}")
            lane.running = False
            lane.remaining = 0
            lane.coins = 0  # ✅ reset all inserted coins
            lane.pending_coin = False

            try:
                sm = self.root.ids.sm
                menu = sm.get_screen("menu")
                if lane_key == "L":
                    menu.ids.timer_label_water.text = "00:00"
                    menu.ids.lane_left.ids.coin_count.text = f"Credit's: 0"
                    self.stop_countdown_beep("L")
                else:
                    menu.ids.timer_label_foaming.text = "00:00"
                    menu.ids.lane_right.ids.coin_count.text = f"Credit's: 0"
                    self.stop_countdown_beep("R")
            except Exception as e:
                safe_log("warning",f"Stop lane UI update error: {e}")

            safe_log("info",f"Lane {lane_key} stopped manually — time and coins cleared.")
        else:
            safe_log("info",f"Stop command ignored for {lane_key} (not running or no coins).")

    def is_lane_running(self, lane_key):
        """Return True if lane is currently active."""
        lane = self.left_lane if lane_key == "L" else self.right_lane
        return lane.running

    def toggle_lane(self, lane_key):
        """Toggle between Start and Stop for a lane."""
        lane = self.left_lane if lane_key == "L" else self.right_lane
        if lane.running:
            # ✅ Show confirmation popup before stopping
            popup = ConfirmStopPopup(lane_key=lane_key)
            popup.open()
        else:
            # Currently idle → start it
            self.start_lane_timer(lane_key)

    # --------------------------
    # Timers
    # --------------------------
    def update_timers(self, dt):
        left_finished = self.left_lane.tick()
        right_finished = self.right_lane.tick()

        # ✅ Check for 10-second warning beep
        self.check_10_second_warning()

        # ✅ Check if running state changed and update video accordingly
        current_left_running = self.left_lane.running
        current_right_running = self.right_lane.running

        # If running state changed, update the video
        if (current_left_running != self.previous_left_running or
                current_right_running != self.previous_right_running):
            self.update_background_video()
            self.previous_left_running = current_left_running
            self.previous_right_running = current_right_running

        try:
            sm = self.root.ids.sm
            if not sm.has_screen("menu"):
                return
            menu = sm.get_screen("menu")

            # ✅ LEFT lane finish handling
            if left_finished or (self.left_lane.remaining <= 0 and self.left_lane.running):
                self.send_serial_command("RELAY_OFF:L")
                self.left_lane.running = False
                self.left_lane.remaining = 0
                self.left_lane.coins = 0
                self.left_lane.pending_coin = False
                # Reset beep state when lane finishes
                self.left_lane_beeped = False
                self.stop_countdown_beep("L")
                menu.ids.timer_label_water.text = "00:00"
                menu.ids.lane_left.ids.coin_count.text = "Credit's: 0"
                safe_log("info","Left lane finished → relay OFF, Credit's cleared")

            # ✅ RIGHT lane finish handling
            if right_finished or (self.right_lane.remaining <= 0 and self.right_lane.running):
                self.send_serial_command("RELAY_OFF:R")
                self.right_lane.running = False
                self.right_lane.remaining = 0
                self.right_lane.coins = 0
                self.right_lane.pending_coin = False
                # Reset beep state when lane finishes
                self.right_lane_beeped = False
                self.stop_countdown_beep("R")
                menu.ids.timer_label_foaming.text = "00:00"
                menu.ids.lane_right.ids.coin_count.text = "Credit's: 0"
                safe_log("info","Right lane finished → relay OFF, coins cleared")

            # ✅ Update display every tick
            menu.ids.timer_label_water.text = self.format_time(self.left_lane.remaining)
            menu.ids.timer_label_foaming.text = self.format_time(self.right_lane.remaining)
            menu.ids.lane_left.ids.coin_count.text = f"Credit's: {self.left_lane.coins}"
            menu.ids.lane_right.ids.coin_count.text = f"Credit's: {self.right_lane.coins}"

            # ✅ Update Start/Stop labels
            menu.ids.lane_left.ids.start_stop_btn.text = (
                "[b]Stop[/b]" if self.left_lane.running else "[b]Start[/b]"
            )
            menu.ids.lane_right.ids.start_stop_btn.text = (
                "[b]Stop[/b]" if self.right_lane.running else "[b]Start[/b]"
            )

            # ✅ Update button color using stored reference
            left_btn = menu.ids.lane_left.ids.start_stop_btn
            right_btn = menu.ids.lane_right.ids.start_stop_btn

            if left_btn.color_instruction:
                left_btn.color_instruction.rgba = (
                    (1.0, 0.3, 0.3, 1.0) if self.left_lane.running else (0.0, 0.592, 0.698, 1.0)
                )
            if right_btn.color_instruction:
                right_btn.color_instruction.rgba = (
                    (1.0, 0.3, 0.3, 1.0) if self.right_lane.running else (0.0, 0.592, 0.698, 1.0)
                )

        except Exception as e:
            safe_log("warning",f"update_timers error: {e}")

        # ✅ Restart inactivity timer when all timers stop
        if not (self.left_lane.running or self.right_lane.running):
            current_screen = self.root.ids.sm.get_screen(self.root.ids.sm.current)
            if isinstance(current_screen, InactivityMixin):
                current_screen.start_inactivity_timer()

    def check_10_second_warning(self):
        """Check if any timer is in last 10 seconds and trigger continuous countdown"""
        # Left lane: 10 seconds or less and running
        if (self.left_lane.running and
            self.left_lane.remaining <= 10 and
            self.left_lane.remaining > 0 and
            not self.left_lane_beeped):

            self.start_countdown_beep("L", self.left_lane.remaining)
            self.left_lane_beeped = True
            safe_log("info",f"Left lane 10-second countdown started: {self.left_lane.remaining}s")

        # Right lane: 10 seconds or less and running
        if (self.right_lane.running and
            self.right_lane.remaining <= 10 and
            self.right_lane.remaining > 0 and
            not self.right_lane_beeped):

            self.start_countdown_beep("R", self.right_lane.remaining)
            self.right_lane_beeped = True
            safe_log("info",f"Right lane 10-second countdown started: {self.right_lane.remaining}s")

        # Reset beep state if timer goes above 10 seconds (manual stop case)
        if self.left_lane.remaining > 10:
            self.left_lane_beeped = False
            self.stop_countdown_beep("L")
        if self.right_lane.remaining > 10:
            self.right_lane_beeped = False
            self.stop_countdown_beep("R")

    def start_countdown_beep(self, lane_key, seconds):
        """Start continuous countdown beeping for lane timers (10 to 0)"""
        if lane_key == "L":
            if hasattr(self, 'left_countdown_event'):
                Clock.unschedule(self.left_countdown_event)
            self.left_countdown_seconds = seconds
            self.left_countdown_event = Clock.schedule_interval(lambda dt: self._update_lane_countdown("L"), 1)
            safe_log("info",f"Left lane countdown beep started: {seconds}s")

        elif lane_key == "R":
            if hasattr(self, 'right_countdown_event'):
                Clock.unschedule(self.right_countdown_event)
            self.right_countdown_seconds = seconds
            self.right_countdown_event = Clock.schedule_interval(lambda dt: self._update_lane_countdown("R"), 1)
            safe_log("info",f"Right lane countdown beep started: {seconds}s")

        # Start first beep immediately
        self._trigger_lane_beep(lane_key)

    def _update_lane_countdown(self, lane_key):
        """Update lane countdown and trigger beeps"""
        try:
            if lane_key == "L":
                if not hasattr(self, 'left_countdown_seconds') or self.left_countdown_seconds <= 0:
                    self.stop_countdown_beep("L")
                    return False

                self.left_countdown_seconds -= 1

                # Update timer color based on remaining seconds
                self._update_lane_timer_color("L", self.left_countdown_seconds)

                # Trigger beep for each second
                self._trigger_lane_beep("L")

                safe_log("debug",f"Left lane countdown: {self.left_countdown_seconds}s")

                if self.left_countdown_seconds <= 0:
                    self.stop_countdown_beep("L")
                    return False

            elif lane_key == "R":
                if not hasattr(self, 'right_countdown_seconds') or self.right_countdown_seconds <= 0:
                    self.stop_countdown_beep("R")
                    return False

                self.right_countdown_seconds -= 1

                # Update timer color based on remaining seconds
                self._update_lane_timer_color("R", self.right_countdown_seconds)

                # Trigger beep for each second
                self._trigger_lane_beep("R")

                safe_log("debug",f"Right lane countdown: {self.right_countdown_seconds}s")

                if self.right_countdown_seconds <= 0:
                    self.stop_countdown_beep("R")
                    return False

            return True

        except Exception as e:
            safe_log("error",f"Lane countdown update error: {e}")
            return False

    def _trigger_lane_beep(self, lane_key):
        """Trigger beep for lane countdown"""
        try:
            # Send serial command to Arduino for beep
            self.send_serial_command("BEEP_ON")

            # Visual feedback - flash the timer label
            self._flash_lane_timer(lane_key)

        except Exception as e:
            safe_log("warning",f"Lane beep trigger failed: {e}")

    def _update_lane_timer_color(self, lane_key, seconds):
        """Update lane timer color based on remaining seconds"""
        try:
            sm = self.root.ids.sm
            if not sm.has_screen("menu"):
                return

            menu = sm.get_screen("menu")

            if lane_key == "L":
                label = menu.ids.timer_label_water
            else:
                label = menu.ids.timer_label_foaming

            # Progressive color changes
            if seconds >= 7:
                label.color = [1, 0.8, 0.2, 1]  # Yellow for 10-7 seconds
            elif seconds >= 4:
                label.color = [1, 0.5, 0.1, 1]  # Orange for 6-4 seconds
            elif seconds >= 1:
                label.color = [1, 0.3, 0.3, 1]  # Red for 3-1 seconds
            else:
                label.color = [1, 0.1, 0.1, 1]  # Dark red for 0 seconds

        except Exception as e:
            safe_log("warning",f"Lane timer color update failed: {e}")

    def _flash_lane_timer(self, lane_key):
        """Flash the lane timer for visual feedback"""
        try:
            sm = self.root.ids.sm
            if not sm.has_screen("menu"):
                return

            menu = sm.get_screen("menu")

            if lane_key == "L":
                label = menu.ids.timer_label_water
            else:
                label = menu.ids.timer_label_foaming

        except Exception as e:
            safe_log("warning",f"Lane timer flash failed: {e}")

    def stop_countdown_beep(self, lane_key):
        """Stop countdown beeping for specified lane"""
        try:
            if lane_key == "L":
                if hasattr(self, 'left_countdown_event'):
                    Clock.unschedule(self.left_countdown_event)
                    self.left_countdown_event = None
                # Reset timer color to normal
                self._reset_lane_timer_color("L")
                safe_log("info","Left lane countdown beep stopped")

            elif lane_key == "R":
                if hasattr(self, 'right_countdown_event'):
                    Clock.unschedule(self.right_countdown_event)
                    self.right_countdown_event = None
                # Reset timer color to normal
                self._reset_lane_timer_color("R")
                safe_log("info","Right lane countdown beep stopped")

        except Exception as e:
            safe_log("error",f"Stop countdown beep failed: {e}")

    def _reset_lane_timer_color(self, lane_key):
        """Reset lane timer color to normal"""
        try:
            sm = self.root.ids.sm
            if not sm.has_screen("menu"):
                return

            menu = sm.get_screen("menu")

            if lane_key == "L":
                label = menu.ids.timer_label_water
            else:
                label = menu.ids.timer_label_foaming

            label.color = [1, 1, 1, 1]  # Reset to white

        except Exception as e:
            safe_log("warning",f"Lane timer color reset failed: {e}")



    def update_background_video(self):
        """Update the background video based on timer states"""
        try:
            sm = self.root.ids.sm
            if sm.has_screen("menu"):
                menu_screen = sm.get_screen("menu")

                # Check if any timer is running
                any_timer_running = self.left_lane.running or self.right_lane.running

                if any_timer_running:
                    menu_screen.switch_to_washing_video()
                else:
                    menu_screen.switch_to_default_video()

        except Exception as e:
            safe_log("warning",f"Background video update error: {e}")

    def start_lane_timer(self, lane_key):
        """Start the lane timer and relay when Start button is pressed."""
        lane = self.left_lane if lane_key == "L" else self.right_lane
        if lane.coins > 0 and not lane.running:
            lane.running = True
            if lane_key == "L":
                self.left_lane_beeped = False
            else:
                self.right_lane_beeped = False

            self.send_serial_command(f"RELAY_ON:{lane_key}")

            # Update background video when timer starts
            self.update_background_video()

            safe_log("info",f"Lane {lane_key} started → timer + relay ON.")
        else:
            safe_log("info",f"Lane {lane_key}: no credit or already running.")

    def format_time(self, seconds):
        m, s = divmod(int(seconds), 60)
        return f"{m:02d}:{s:02d}"

    # --------------------------
    # Firebase and Local
    # --------------------------

    def save_account_data(self, lane_key, amount):
        data = {}
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r") as f:
                    data = json.load(f)
            except json.JSONDecodeError:
                pass

        if lane_key == "L":
            data["water_coins"] = data.get("water_coins", 0) + int(amount)
        elif lane_key == "R":
            data["foaming_coins"] = data.get("foaming_coins", 0) + int(amount)

        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=4)

        threading.Thread(target=self.background_sync_to_firebase, daemon=True).start()

    def background_sync_to_firebase(self):
        """Sync the full local totals (not increments) to Firebase."""
        if not os.path.exists(DATA_FILE) or db is None:
            return

        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
        except json.JSONDecodeError:
            safe_log("warning","Invalid JSON file.")
            return

        # Skip sync if offline
        if not self.is_connected():
            safe_log("info","Offline, Firebase sync postponed.")
            return

        try:
            total_water = data.get("water_coins", 0)
            total_foam = data.get("foaming_coins", 0)
            total_earnings = total_water + total_foam

            machine_ref = db.collection("machines").document(MACHINE_ID)

            # ✅ Write absolute totals from local JSON
            machine_ref.set({
                "ownerId": OWNER_ID,
                "location": LOCATION,
                "machine_name": "Carwash Bay 1",
                "water_coins": total_water,
                "foaming_coins": total_foam,
                "total_earnings": total_earnings,
                "updated_at": firestore.SERVER_TIMESTAMP
            }, merge=True)

            # ✅ Optional: Add one transaction for the last coin (with lane tracking)
            last_lane = None
            if total_water or total_foam:
                if total_water >= total_foam:
                    last_lane = "L"
                else:
                    last_lane = "R"


            safe_log("info",
                f"Firebase sync success → totals: water={total_water}, foam={total_foam}, total={total_earnings}")

        except Exception as e:
            safe_log("warning",f"Firebase sync failed: {e}")

    def is_connected(self):
        """Check if internet is available."""
        try:
            requests.get("https://clients3.google.com/generate_204", timeout=2)
            return True
        except requests.RequestException:
            return False

    def start_realtime_sync(self):
        """Continuous background sync of totals every 2 minutes."""

        def sync_loop():
            while True:
                time.sleep(120)
                self.background_sync_to_firebase()

        threading.Thread(target=sync_loop, daemon=True).start()

    def on_stop(self):
        self.serial_alive = False

        if getattr(self, "serial_port", None) and getattr(self.serial_port, "is_open", False):
            try:
                self.serial_port.close()
            except Exception:
                pass

    # ───────────────────────────────────────────────
    # FIREBASE COMMAND LISTENER (Restart / Shutdown)
    # ───────────────────────────────────────────────
    def listen_for_commands(self):
        """Continuously listens to Firestore 'commands' collection for restart/shutdown commands."""
        if db is None:
            safe_log("warning","Firestore not initialized — skipping command listener.")
            return

        machine_ref = db.collection("machines").document(MACHINE_ID)
        commands_ref = machine_ref.collection("commands")

        def on_snapshot(col_snapshot, changes, read_time):
            """Handle incoming Firestore command changes."""
            if not changes:
                return

            for change in changes:
                if change.type.name != "ADDED":
                    continue

                doc_id = change.document.id
                cmd = change.document.to_dict() or {}
                cmd_type = cmd.get("type", "").lower().strip()
                safe_log("info",f"📥 Firestore command received: {cmd_type}")


                try:
                    # ✅ Always delete the command FIRST
                    commands_ref.document(doc_id).delete()
                    safe_log("info",f"🗑️ Command '{cmd_type}' deleted immediately before execution.")

                    # ────────────── EXECUTE COMMAND ──────────────
                    if cmd_type in ("restart_device", "restart_pi", "reboot"):
                        safe_log("info","⚙️ Restart device command detected — rebooting Raspberry Pi...")
                        subprocess.Popen(
                            ["/usr/bin/sudo", "reboot"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )

                    elif cmd_type in ("restart_app", "app_restart"):
                        safe_log("info","🔄 Restart App command detected — restarting via systemd...")
                        subprocess.Popen(
                            ["/usr/bin/sudo", "systemctl", "restart", "carwash.service"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )

                    elif cmd_type in ("shutdown", "poweroff"):
                        safe_log("info","🛑 Shutdown command detected — powering off...")
                        subprocess.Popen(
                            ["/usr/bin/sudo", "shutdown", "now"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )

                    else:
                        safe_log("info",f"⚠️ Unknown command '{cmd_type}' ignored.")

                except Exception as e:
                    safe_log("error",f"⚠️ Command execution failed: {e}")

        def attach_listener():
            """Attach a Firestore snapshot listener and auto-recover on failure."""
            try:
                safe_log("info","📡 Attaching Firestore command listener...")
                commands_ref.on_snapshot(on_snapshot)
                safe_log("info","🔥 Firestore command listener started successfully.")
            except Exception as e:
                safe_log("error",f"❌ Failed to attach Firestore listener: {e}")

                safe_log("info","🔁 Retrying listener in 10 seconds...")
                Clock.schedule_once(lambda dt: attach_listener(), 10)

        # 🔁 Start and monitor listener in a background thread
        def listener_loop():
            while True:
                try:
                    attach_listener()
                    # Wait 5 minutes before reattaching just in case Firestore drops silently
                    time.sleep(300)
                except Exception as e:
                    safe_log("error",f"Listener loop crashed: {e}")

                    time.sleep(10)

        threading.Thread(target=listener_loop, daemon=True).start()


if __name__ == "__main__":
    CarwashApp().run()