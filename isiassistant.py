#!/usr/bin/env python3
"""
Push-to-talk voice assistant for Raspberry Pi
  Mic -> Groq Whisper (English only) -> LLM (streaming) -> gTTS -> USB speaker
  + animated 128x64 SH1106 OLED (face, level meter, scrolling text)

Run (keyboard library needs root, -E keeps your environment variables):
    export GROQ_API_KEY="your_new_key"
    sudo -E python3 assistant.py
"""
import os
import sys
import re
import math
import unicodedata
import time
import wave
import queue
import fcntl
import select
import shutil
import signal
import tempfile
import threading
import atexit
import termios
import subprocess
from collections import Counter, deque
from math import gcd

import numpy as np
import smbus2
from PIL import Image, ImageDraw, ImageFont
from groq import Groq
from gtts import gTTS

try:
    import evdev                      # preferred key reader: pip install evdev
except ImportError:
    evdev = None

try:
    import keyboard                   # fallback key reader
except ImportError:
    keyboard = None

try:
    import ollama
except ImportError:
    ollama = None

try:
    from scipy.signal import resample_poly  # better resampling (pip install scipy)
except ImportError:
    resample_poly = None

os.environ["JACK_NO_START_SERVER"] = "1"
os.environ["PYTHONWARNINGS"] = "ignore"

# Ctrl+C is handled by ONE dedicated thread (see "shutdown" below). Blocking the signals in every
# other thread means Ctrl+C still works even if the main thread is stuck inside an audio driver.
signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})

# =============================================================================
# 1. CONFIGURATION
# =============================================================================
def load_env_file(path):
    """Read KEY=VALUE lines from a .env file into the environment (a real environment variable wins)."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip().removeprefix("export ").strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
    except OSError:
        pass


load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))   # .env next to this script
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
if not GROQ_API_KEY:
    sys.exit("ERROR: GROQ_API_KEY not found. Create a file named .env next to this script containing:\n"
             "GROQ_API_KEY=your_key_here")

PTT_KEY = "e"

# --- Audio devices are found AUTOMATICALLY by NAME, so the USB port does not matter ---
# (names come from your `lsusb`; lowercase, a device matches if ANY hint appears in its name)
MIC_HINTS = ("pcm2902", "usb audio codec")      # Texas Instruments PCM2902 (08bb:2902)
SPEAKER_HINTS = ("uacdemo", "jieli")            # Jieli UACDemoV1.0 (4c4a:4155)
MIC_DEVICE_OVERRIDE = os.environ.get("MIC_DEVICE")      # optional, e.g. "plughw:1,0"
SPEAKER_OVERRIDE = os.environ.get("SPEAKER_DEVICE")     # optional, e.g. "plughw:2,0"

# --- Speech-to-text ---
STT_MODEL = "whisper-large-v3"               # more accurate than the -turbo version
STT_LANGUAGE = "en"                          # force English (no auto-detect mistakes)
STT_PROMPT = ("Hello. This is a conversation with a voice assistant. "
              "Transcribe in English with correct spelling, capitalization and punctuation. "
              "Names that may appear: ISI, Institut Superieur d'Informatique, Ariana, Tunis El Manar, "
              "LIMTIC, GLSI, IDL, IDISC, ISEOC, SSII.")
STT_RATE = 16000                             # Whisper's native rate
MAX_RECORD_SECONDS = 300                     # 5 min of speech (~10 MB at 16 kHz)
MIN_RECORD_SECONDS = 0.4
PREROLL_SECONDS = 0.3                        # audio kept from just BEFORE you press the key

# --- LLM ---
LLM_BACKEND = os.environ.get("LLM_BACKEND", "ollama")   # "ollama" (local) or "groq" (cloud, much smarter)
OLLAMA_MODEL = "gemma3:1b"                   # try gemma3:4b on a Pi 5 with 8 GB
GROQ_LLM_MODEL = "llama-3.3-70b-versatile"
LLM_MAX_TOKENS = 450                         # ~300 words -> about 2 minutes of speech
LLM_TEMPERATURE = 0.5
MAX_HISTORY_TURNS = 6                        # conversation memory (user+assistant pairs)
OLLAMA_THREADS = None                        # e.g. 3 on a 4-core Pi: leaves one core free for audio

# --- Speech output ---
TTS_RATE = 24000                             # gTTS audio is 24 kHz mono
PREBUFFER_SENTENCES = 2                      # start talking when this many sentences are ready (1 = faster start)
PREBUFFER_MAX_WAIT = 6.0                     # ...but never wait longer than this for the 2nd sentence
AUDIO_BUFFER_US = 300000                     # speaker buffer; raise to 500000 if you still hear crackles
PIPE_BYTES = 8192

# --- School knowledge (ISI) ---
KNOWLEDGE_FILE = os.environ.get("KNOWLEDGE_FILE",
                                os.path.join(os.path.dirname(os.path.abspath(__file__)), "isi_knowledge.txt"))
KB_MAX_CHUNKS = 3                            # at most this many knowledge entries per question
KB_MAX_CHARS = 1600                          # ...and at most this much text (keeps the Pi fast)
OLLAMA_NUM_CTX = 4096                        # room for system prompt + facts + history + answer
HIST_REPLY_CHARS = 500                       # older answers are shortened to this when re-sent
HIST_MAX_CHARS = 3200                        # total size of remembered conversation sent to the model

SYSTEM_PROMPT = (
    "You are a friendly, knowledgeable voice assistant running on a Raspberry Pi. "
    "Your answers are read aloud, so follow these rules strictly:\n"
    "1. ALWAYS reply in clear, natural English, even if the user's words look like another language.\n"
    "2. Speak naturally in plain sentences. No markdown, bullet points, lists, emojis, or special symbols.\n"
    "3. For simple questions answer in 1-3 sentences. For questions that need explanation, "
    "give a fuller answer of about 5-8 sentences.\n"
    "4. If the request is unclear or the transcript looks garbled, ask a short clarifying question.\n"
    "5. Be accurate. If you do not know something, say so.\n"
    "6. You are the voice assistant of ISI, the Institut Superieur d'Informatique, a public computer science "
    "institute in Ariana, Tunisia, part of the University of Tunis El Manar. When the user says 'your school', "
    "'your university', 'your institute' or 'ISI', they mean this institute. For general questions answer as usual. "
    "For questions about ISI, answer only from the facts given with the question; keep names, numbers and acronyms "
    "exactly as given. If the facts do not contain the answer, say you do not have that detail and suggest "
    "contacting ISI at isi at isi dot U T M dot T N or on the phone at plus 216 71 706 164."
)

# --- OLED ---
OLED_I2C_BUS = 1
OLED_ADDR = 0x3C
W, H = 128, 64
BODY_TOP = 16
LINE_H = 10
VISIBLE_LINES = 4
FPS = 10

TMP_DIR = tempfile.mkdtemp(prefix="assistant_")
CHUNK = 1024


# =============================================================================
# 2. OLED DISPLAY (animated, runs in its own thread)
# =============================================================================
def load_font():
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"):
        if os.path.exists(path):
            return ImageFont.truetype(path, 9)
    return ImageFont.load_default()


class OLED:
    LABELS = {"INIT": "STARTING", "IDLE": "READY", "LISTEN": "LISTENING",
              "THINK": "THINKING", "TALK": "SPEAKING", "ERROR": "OOPS"}

    def __init__(self):
        self.bus = smbus2.SMBus(OLED_I2C_BUS)
        self.i2c_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.font = load_font()
        self._scratch = ImageDraw.Draw(Image.new("1", (1, 1)))

        self.state = "INIT"
        self.lines = []
        self.text_t0 = time.time()
        self.text_duration = None
        self.state_t0 = time.time()
        self.level = 0.0

        self._prev_pages = [None] * 8
        self._running = True
        self._init_panel()
        threading.Thread(target=self._loop, daemon=True).start()

    # ---------- low level ----------
    def _cmd(self, cmd):
        try:
            with self.i2c_lock:
                self.bus.write_byte_data(OLED_ADDR, 0x00, cmd)
        except OSError as e:
            print(f"OLED write error: {e}", flush=True)

    def _init_panel(self):
        for c in [0xAE, 0xD5, 0x80, 0xA8, 0x3F, 0xD3, 0x00, 0x40, 0xAD, 0x8B,
                  0xA1, 0xC8, 0xDA, 0x12, 0x81, 0xD0, 0xD9, 0x22, 0xDB, 0x40,
                  0x33, 0xA4, 0xA6, 0xAF]:
            self._cmd(c)

    def _push(self, img):
        """Pack the 1-bit image into SH1106 pages and send ONLY pages that changed."""
        arr = (np.asarray(img.convert("L")) > 0).astype(np.uint8).reshape(8, 8, W)
        buf = np.zeros((8, W), dtype=np.uint8)
        for r in range(8):
            buf |= (arr[:, r, :] * (1 << r)).astype(np.uint8)
        for page in range(8):
            row = buf[page].tobytes()
            if row == self._prev_pages[page]:
                continue
            try:
                with self.i2c_lock:
                    self.bus.write_i2c_block_data(OLED_ADDR, 0x00, [0xB0 + page, 0x02, 0x10])
                    data = list(row)
                    for i in range(0, W, 32):
                        self.bus.write_i2c_block_data(OLED_ADDR, 0x40, data[i:i + 32])
                self._prev_pages[page] = row
            except OSError as e:
                print(f"OLED I2C error: {e}", flush=True)
                self._prev_pages[page] = None
                return

    # ---------- public API ----------
    def set_state(self, state, text=None, duration=None):
        """state: INIT/IDLE/LISTEN/THINK/TALK/ERROR.  text is wrapped + auto-scrolled.
        duration (seconds) makes the scroll finish in that time (use for speech)."""
        lines = self._wrap(text) if text else []
        with self.state_lock:
            self.lines = lines
            self.text_t0 = time.time()
            self.text_duration = duration
            if state != self.state:
                self.state_t0 = time.time()
            self.state = state

    def set_level(self, level):
        level = max(0.0, min(1.0, level))
        self.level = level if level > self.level else self.level * 0.6 + level * 0.4

    def stop(self):
        self._running = False
        time.sleep(0.15)
        self._prev_pages = [None] * 8
        self._push(Image.new("1", (W, H), 0))

    # ---------- text helpers ----------
    def _tlen(self, s):
        return self._scratch.textlength(s, font=self.font)

    def _wrap(self, text, max_w=114):
        lines = []
        for para in text.split("\n"):
            cur = ""
            for word in para.split():
                test = f"{cur} {word}".strip()
                if self._tlen(test) <= max_w:
                    cur = test
                    continue
                if cur:
                    lines.append(cur)
                while self._tlen(word) > max_w and len(word) > 1:  # very long word
                    cut = len(word)
                    while cut > 1 and self._tlen(word[:cut]) > max_w:
                        cut -= 1
                    lines.append(word[:cut])
                    word = word[cut:]
                cur = word
            if cur:
                lines.append(cur)
        return lines

    def _center(self, d, text, y):
        d.text(((W - self._tlen(text)) / 2, y), text, font=self.font, fill=1)

    def _offset(self, n_lines, now, t0, duration):
        max_off = max(0, n_lines - VISIBLE_LINES)
        if max_off == 0:
            return 0
        t = now - t0
        if duration:
            return int(round(min(max(t / duration, 0.0), 1.0) * max_off))
        step = int(t / 1.0) % (max_off + 4)       # hold 2 ticks at start, 1 at end, then loop
        return min(max(step - 2, 0), max_off)

    # ---------- rendering ----------
    def _loop(self):
        while self._running:
            t = time.time()
            try:
                self._push(self._render(t))
            except Exception as e:
                print(f"OLED render error: {e}", flush=True)
            time.sleep(max(0.0, 1.0 / FPS - (time.time() - t)))

    def _render(self, now):
        with self.state_lock:
            state, lines = self.state, self.lines
            t0, dur, s0 = self.text_t0, self.text_duration, self.state_t0

        img = Image.new("1", (W, H), 0)
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, W - 1, H - 1), outline=1)
        d.rectangle((0, 0, W - 1, 12), fill=1)
        d.text((4, 1), self.LABELS.get(state, state), font=self.font, fill=0)
        self._header_widget(d, state, now, s0)

        if state in ("THINK", "TALK") and lines:
            self._draw_text(d, lines, now, t0, dur)
        else:
            self._draw_face(d, state, now, lines[0] if lines else None)
        return img

    def _header_widget(self, d, state, now, s0):
        if state == "LISTEN":
            if int(now * 2) % 2 == 0:
                d.ellipse((115, 3, 121, 9), fill=0)                    # blinking REC dot
            secs = int(now - s0)
            d.text((82, 1), f"{secs // 60}:{secs % 60:02d}", font=self.font, fill=0)
        elif state == "THINK":
            active = int(now * 4) % 3
            for i in range(3):
                r = 2 if i == active else 1
                cx = 104 + i * 8
                d.ellipse((cx - r, 6 - r, cx + r, 6 + r), fill=0)
        elif state == "TALK":
            for i in range(4):                                           # equalizer bars
                h = 2 + int(abs(math.sin(now * 9 + i * 1.7)) * 8)
                d.rectangle((100 + i * 6, 11 - h, 103 + i * 6, 11), fill=0)

    def _draw_text(self, d, lines, now, t0, dur):
        n = len(lines)
        off = self._offset(n, now, t0, dur)
        for i, line in enumerate(lines[off:off + VISIBLE_LINES]):
            d.text((5, BODY_TOP + i * LINE_H), line, font=self.font, fill=1)
        if n > VISIBLE_LINES:                                            # scrollbar
            track = VISIBLE_LINES * LINE_H
            thumb = max(5, int(track * VISIBLE_LINES / n))
            y = BODY_TOP + int((off / (n - VISIBLE_LINES)) * (track - thumb))
            d.line((124, BODY_TOP, 124, BODY_TOP + track), fill=1)
            d.rectangle((123, y, 125, y + thumb), fill=1)

    def _draw_face(self, d, state, now, caption):
        if state == "ERROR":
            for x0 in (26, 80):
                d.line((x0, 20, x0 + 20, 40), fill=1, width=3)
                d.line((x0 + 20, 20, x0, 40), fill=1, width=3)
        else:
            blink = (now % 4.0) < 0.15
            eye_w, eye_h, top = 22, 26, 17
            dx = dy = 0
            if state == "IDLE":
                dx = int(4 * math.sin(now * 0.7))                         # eyes look around
            elif state == "LISTEN":
                eye_h = 22 + int(self.level * 10)                         # eyes widen with your voice
            elif state in ("THINK", "TALK"):
                dy = -3
            h, y = (3, top + (eye_h - 3) // 2) if blink else (eye_h, top)
            for x0 in (26, 80):
                d.rounded_rectangle((x0 + dx, y + dy, x0 + dx + eye_w, y + dy + h),
                                    radius=min(6, h // 2), fill=1)
            if state == "IDLE":
                d.arc((54, 36, 74, 48), 20, 160, fill=1, width=2)        # smile

        if state == "LISTEN":                                             # live mic level meter
            d.rectangle((9, 53, 118, 58), outline=1)
            d.rectangle((10, 54, 10 + int(self.level * 107), 57), fill=1)
        else:
            default = {"IDLE": f"Hold [{PTT_KEY.upper()}] to talk", "INIT": "Starting...",
                       "ERROR": "Something went wrong"}.get(state, "")
            self._center(d, (caption or default)[:24], 49)


# =============================================================================
# 3. SETUP
# =============================================================================
oled = OLED()
oled.set_state("INIT")

# --- Push-to-talk key tracking ---------------------------------------------
# `ptt_down` is set while the PTT key is held. Every other key is ignored completely.
# Preferred reader: evdev (reads /dev/input directly). It re-scans every 2 s, so a keyboard that
# resets or is re-plugged is picked up again automatically.
ptt_down = threading.Event()
KEY_DEBUG = bool(os.environ.get("KEY_DEBUG"))


def _evdev_watcher():
    ecodes = evdev.ecodes
    key_code = getattr(ecodes, f"KEY_{PTT_KEY.upper()}")
    devices, last_scan = {}, 0.0
    while True:
        try:
            if time.time() - last_scan > 2.0:
                last_scan = time.time()
                for path in evdev.list_devices():
                    if path in devices:
                        continue
                    try:
                        dev = evdev.InputDevice(path)
                        if key_code in dev.capabilities().get(ecodes.EV_KEY, []):
                            devices[path] = dev
                            print(f"-> Keyboard attached: {dev.name}", flush=True)
                        else:
                            dev.close()
                    except OSError:
                        pass
            if not devices:
                time.sleep(0.5)
                continue
            ready, _, _ = select.select(list(devices.values()), [], [], 0.5)
            for dev in ready:
                try:
                    for ev in dev.read():
                        if ev.type == ecodes.EV_KEY and ev.code == key_code:
                            if KEY_DEBUG:
                                print(f"[key {PTT_KEY.upper()} value={ev.value}]", flush=True)
                            if ev.value == 1:
                                ptt_down.set()
                            elif ev.value == 0:
                                ptt_down.clear()
                except BlockingIOError:
                    pass
                except OSError:                      # keyboard unplugged or reset
                    print(f"-> Keyboard lost: {dev.name} (will re-scan)", flush=True)
                    devices.pop(dev.path, None)
                    ptt_down.clear()
                    try:
                        dev.close()
                    except Exception:
                        pass
        except Exception as e:
            print(f"Key watcher error: {e}", flush=True)
            time.sleep(1.0)


def _keyboard_lib_hook():
    try:
        codes = set(keyboard.key_to_scan_codes(PTT_KEY))
    except Exception:
        codes = set()

    def on_key(event):
        try:
            if event.name == PTT_KEY or event.scan_code in codes:
                if event.event_type == keyboard.KEY_DOWN:
                    ptt_down.set()
                elif event.event_type == keyboard.KEY_UP:
                    ptt_down.clear()
        except Exception:
            pass

    keyboard.hook(on_key)


if evdev is not None:
    threading.Thread(target=_evdev_watcher, daemon=True).start()
    print("-> Keys: evdev (auto re-scans keyboards)", flush=True)
elif keyboard is not None:
    _keyboard_lib_hook()
    print("-> Keys: 'keyboard' library (install evdev for a more reliable reader)", flush=True)
else:
    sys.exit("No key reader available. Install one:  pip install evdev")

# --- Terminal hygiene -------------------------------------------------------
# Other keys you press would be typed into the terminal (echoed, and run as shell
# commands after the program exits). Turn echo off while running and discard them.
_term_attrs = None


def quiet_terminal():
    global _term_attrs
    try:
        fd = sys.stdin.fileno()
        _term_attrs = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] &= ~(termios.ECHO | termios.ICANON)
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
    except Exception:
        _term_attrs = None


def restore_terminal():
    if _term_attrs is not None:
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _term_attrs)
        except Exception:
            pass


def flush_typed_keys():
    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        pass


quiet_terminal()
atexit.register(restore_terminal)


current_player = None          # the running speaker (aplay) process, killed on exit
_mic_proc = None               # the running microphone (arecord) process, killed on exit


def _cleanup_display():
    try:
        oled.stop()
    except Exception:
        pass


def shutdown(sig=None, frame=None):
    print("\nShutting down assistant...", flush=True)
    restore_terminal()
    for p in (current_player, _mic_proc):
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass
    t = threading.Thread(target=_cleanup_display, daemon=True)
    t.start()
    t.join(1.5)                                  # never hang while exiting
    os._exit(0)


def _signal_waiter():
    signal.sigwait({signal.SIGINT, signal.SIGTERM})
    shutdown()


threading.Thread(target=_signal_waiter, daemon=True).start()

# Safety net: if an audio library re-enables Ctrl+C in the main thread, handle it the same way
signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

# --- Automatic speaker detection (by ALSA card NAME, not card number) ---------
def _alsa_cards(path="/proc/asound/cards"):
    """Returns [(number, card_id, description_lowercase)] for every sound card."""
    cards = []
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return cards
    for i, line in enumerate(lines):
        m = re.match(r"\s*(\d+)\s+\[(\S+)\s*\]:\s*(.*)", line)
        if m:
            extra = lines[i + 1].strip() if i + 1 < len(lines) else ""
            cards.append((int(m.group(1)), m.group(2), f"{m.group(3)} {extra}".lower()))
    return cards


def find_speaker(cards=None):
    """ALSA device string for the USB speaker. Uses the card's NAME (plughw:CARD=<id>),
    which stays the same no matter which USB port or order you plug things in."""
    if SPEAKER_OVERRIDE:
        return SPEAKER_OVERRIDE
    cards = _alsa_cards() if cards is None else cards
    for num, cid, desc in cards:
        if any(h in desc or h in cid.lower() for h in SPEAKER_HINTS):
            return f"plughw:CARD={cid},DEV=0"
    for num, cid, desc in cards:                      # fallback: any USB card that is not the mic
        if "usb" in desc and not any(h in desc for h in MIC_HINTS):
            return f"plughw:CARD={cid},DEV=0"
    return "default"


# --- Automatic microphone detection (by ALSA card NAME, same idea as the speaker) --------------
# The microphone is read through `arecord`, a separate program. If the mic stalls or is unplugged, the
# program just kills and restarts it (cleanly frees the USB device) - no audio-library state to corrupt.
MIC_RATE = STT_RATE                 # arecord converts to 16 kHz mono itself, so no resampling is needed
MIC_SR = MIC_RATE
MIC_CHUNK = 2048                    # bytes per read (about 64 ms)
MIC_STALL_SECONDS = 3.0             # no audio for this long -> restart the microphone
mic_ready = threading.Event()       # set while audio is flowing

_mic_proc = None
_rec_lock = threading.Lock()
_preroll = deque(maxlen=max(1, int(PREROLL_SECONDS * MIC_RATE * 2 / MIC_CHUNK)))
_rec_frames = []
_recording = False
_last_audio = time.time()


def find_mic(cards=None, has_capture=None):
    """ALSA device string of the microphone: plughw:CARD=<name>,DEV=0 (stays valid on any USB port)."""
    if MIC_DEVICE_OVERRIDE:
        return MIC_DEVICE_OVERRIDE
    cards = _alsa_cards() if cards is None else cards
    has_capture = has_capture or (lambda n: os.path.exists(f"/proc/asound/card{n}/pcm0c"))
    usable = [(n, cid, d) for n, cid, d in cards if has_capture(n)]
    for n, cid, d in usable:                                          # 1) the PCM2902 by name
        if any(h in d or h in cid.lower() for h in MIC_HINTS):
            return f"plughw:CARD={cid},DEV=0"
    for n, cid, d in usable:                                          # 2) any other USB mic (not the speaker)
        if "usb" in d and not any(h in d or h in cid.lower() for h in SPEAKER_HINTS):
            return f"plughw:CARD={cid},DEV=0"
    return None                                                        # not plugged in


def _feed_audio(data):
    with _rec_lock:
        if _recording:
            _rec_frames.append(data)
        else:
            _preroll.append(data)
    if _recording:
        x = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        oled.set_level(float(np.sqrt(np.mean(x ** 2))) / 6000.0)


def _mic_reader():
    """Thread: keeps an arecord process running and feeds its audio into the buffers."""
    global _mic_proc, _last_audio
    announced = None
    while True:
        dev = find_mic()
        if not dev:
            mic_ready.clear()
            if announced != "missing":
                print("No microphone found - plug it in. Retrying...", flush=True)
                announced = "missing"
            time.sleep(2.0)
            continue
        try:
            proc = subprocess.Popen(
                ["arecord", "-q", "-D", dev, "-t", "raw", "-f", "S16_LE", "-r", str(MIC_RATE), "-c", "1",
                 "--buffer-time=500000"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            print("ERROR: 'arecord' not found. Install it:  sudo apt install alsa-utils", flush=True)
            time.sleep(5.0)
            continue
        _last_audio = time.time()
        _mic_proc = proc
        if announced != dev:
            print(f"-> Microphone: {dev}", flush=True)
            announced = dev
        buf = b""
        try:
            fd = proc.stdout.fileno()
            while True:
                d = os.read(fd, MIC_CHUNK)
                if not d:                                             # arecord ended (unplugged / killed)
                    break
                _last_audio = time.time()
                mic_ready.set()
                buf += d
                n = len(buf) // 2 * 2
                if n:
                    _feed_audio(buf[:n])
                    buf = buf[n:]
        except Exception:
            pass
        mic_ready.clear()
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
        print("Microphone stream ended - reconnecting...", flush=True)
        time.sleep(1.0)


def _mic_watchdog():
    """Thread: if audio stops flowing, kill arecord; the reader then starts a fresh one."""
    while True:
        time.sleep(0.5)
        p = _mic_proc
        if p is not None and p.poll() is None and time.time() - _last_audio > MIC_STALL_SECONDS:
            print("Microphone stalled - restarting it...", flush=True)
            try:
                p.kill()
            except Exception:
                pass


def start_mic():
    threading.Thread(target=_mic_reader, daemon=True).start()
    threading.Thread(target=_mic_watchdog, daemon=True).start()
    for _ in range(50):
        if mic_ready.wait(0.1):
            print("-> Microphone ready", flush=True)
            return
    print("WARNING: no audio from the microphone yet (still trying)...", flush=True)


groq_client = Groq(api_key=GROQ_API_KEY, timeout=30.0, max_retries=1)
if LLM_BACKEND == "ollama" and ollama is None:
    sys.exit("LLM_BACKEND=ollama but the 'ollama' package is not installed.")
print(f"-> STT: {STT_MODEL} (language={STT_LANGUAGE}) | LLM: {LLM_BACKEND}", flush=True)


# =============================================================================
# 4. RECORDING + AUDIO CLEANUP
# =============================================================================
def record_ptt():
    """Wait for the key, then record until it is released. The mic callback keeps a short
    pre-roll buffer, so the first word is never clipped. Returns raw mono int16 bytes."""
    global _recording
    shown_problem = False
    while not ptt_down.wait(0.05):          # short waits: Ctrl+C stays responsive
        problem = not mic_ready.is_set()
        if problem != shown_problem:        # show on the OLED while the mic is being restarted
            oled.set_state("ERROR", "Mic reconnecting") if problem else oled.set_state("IDLE")
            shown_problem = problem

    with _rec_lock:
        _rec_frames.clear()
        _rec_frames.extend(_preroll)
        _recording = True
    oled.set_state("LISTEN")
    print(f"\n[Recording] speak now... release '{PTT_KEY}' when done", flush=True)

    t0 = time.time()
    try:
        while ptt_down.is_set():
            if time.time() - t0 > MAX_RECORD_SECONDS:
                print("[Max recording length reached]", flush=True)
                ptt_down.clear()
                break
            time.sleep(0.03)
    finally:
        with _rec_lock:
            _recording = False
            raw = b"".join(_rec_frames)
            _rec_frames.clear()
    oled.set_level(0)
    return raw


def resample(a, sr_in, sr_out=STT_RATE):
    if sr_in == sr_out:
        return a
    if resample_poly is not None:
        g = gcd(sr_in, sr_out)
        return resample_poly(a, sr_out // g, sr_in // g).astype(np.float32)
    n_out = int(len(a) * sr_out / sr_in)
    return np.interp(np.linspace(0, len(a) - 1, n_out), np.arange(len(a)), a).astype(np.float32)


def prepare_audio(raw):
    """Clean the recording for Whisper: remove DC offset, resample to 16 kHz,
    trim silence at both ends, normalise volume. Returns (wav_path, seconds) or (None, 0)."""
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if len(a) < MIN_RECORD_SECONDS * MIC_SR:
        return None, 0
    a = a - a.mean()
    a = resample(a, MIC_SR)

    frame = int(0.02 * STT_RATE)
    n = len(a) // frame
    if n == 0:
        return None, 0
    rms = np.sqrt(np.mean(a[:n * frame].reshape(n, frame) ** 2, axis=1))
    thr = max(0.006, rms.max() * 0.08)
    voiced = np.where(rms > thr)[0]
    if len(voiced) == 0:
        return None, 0
    start = max(0, (voiced[0] - 10) * frame)               # keep 200 ms padding
    end = min(len(a), (voiced[-1] + 11) * frame)
    a = a[start:end]

    peak = float(np.abs(a).max())
    if peak < 0.005:
        return None, 0
    a = a * min(0.9 / peak, 12.0)                           # normalise, limit gain

    duration = len(a) / STT_RATE
    if duration < MIN_RECORD_SECONDS:
        return None, 0
    path = os.path.join(TMP_DIR, "utterance.wav")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(STT_RATE)
        wf.writeframes((a * 32767).astype(np.int16).tobytes())
    return path, duration


# =============================================================================
# 5. SPEECH-TO-TEXT
# =============================================================================
HALLUCINATIONS = {"thank you", "thanks", "thanks for watching", "you", "bye", "okay"}


def transcribe(path, duration):
    try:
        with open(path, "rb") as f:
            data = f.read()
        tr = groq_client.audio.transcriptions.create(
            file=("utterance.wav", data),
            model=STT_MODEL,
            language=STT_LANGUAGE,          # transcribe English, never auto-detect / translate
            prompt=STT_PROMPT,              # nudges spelling + punctuation style
            temperature=0.0,                # deterministic = fewer random errors
            response_format="verbose_json",
        )
    except Exception as e:
        print(f"Groq STT error: {e}", flush=True)
        return ""

    # Drop segments Whisper itself flags as "probably not speech" (kills phantom text)
    parts = []
    segments = getattr(tr, "segments", None) or []
    for s in segments:
        get = s.get if isinstance(s, dict) else (lambda k, default=None, _s=s: getattr(_s, k, default))
        if get("no_speech_prob", 0) > 0.6 and get("avg_logprob", 0) < -0.8:
            continue
        parts.append((get("text", "") or "").strip())
    text = " ".join(parts).strip() if segments else (tr.text or "").strip()

    if duration < 1.5 and text.lower().strip(" .!?,") in HALLUCINATIONS:
        return ""
    return text


# =============================================================================
# 6. LLM (streaming) + TEXT-TO-SPEECH PIPELINE
# =============================================================================
SENT_END = re.compile(r"(?<=[.!?])\s+|\n+")
MIN_SENT_CHARS = 20
_SMART = str.maketrans({"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
                        "\u2013": "-", "\u2014": "-", "\u2026": "..."})


def clean_for_speech(text):
    text = text.translate(_SMART)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"^\s*[-\u2022]\s+", "", text, flags=re.M)
    text = re.sub(r"[*_#`>~|\[\]{}]", "", text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()   # keep e from e-acute; no emojis
    return re.sub(r"\s+", " ", text).strip()


def stream_llm(messages):
    if LLM_BACKEND == "groq":
        stream = groq_client.chat.completions.create(
            model=GROQ_LLM_MODEL, messages=messages, stream=True,
            max_tokens=LLM_MAX_TOKENS, temperature=LLM_TEMPERATURE)
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
    else:
        for part in ollama.chat(
                model=OLLAMA_MODEL, messages=messages, stream=True, keep_alive="30m",
                options={"num_predict": LLM_MAX_TOKENS, "temperature": LLM_TEMPERATURE,
                         "num_ctx": OLLAMA_NUM_CTX,
                         **({"num_thread": OLLAMA_THREADS} if OLLAMA_THREADS else {})}):
            piece = part["message"]["content"]
            if piece:
                yield piece


def sentences_from_stream(chunks, stop_event):
    """Turn a stream of text chunks into whole sentences as soon as they are complete."""
    buf = ""
    for chunk in chunks:
        if stop_event.is_set():
            return
        buf += chunk
        while True:
            cut = next((m for m in SENT_END.finditer(buf) if m.start() >= MIN_SENT_CHARS), None)
            if cut is None:
                break
            sent, buf = buf[:cut.start()].strip(), buf[cut.end():]
            if sent:
                yield sent
    if buf.strip() and not stop_event.is_set():
        yield buf.strip()


# =============================================================================
# 6b. SCHOOL KNOWLEDGE (ISI): small keyword search, no extra packages needed
# =============================================================================
_KB_STOP = set("""a an the is are was were be been am do does did of in on at to for from with and or but not no yes
it its this that these those i you me we our they them he she his her what which who whom whose when where why how can
could would should will shall may might about tell please give any some there so if then than as by into over out up
down also very just more most much many here your yours my have has had get offer offers available provide
exist want like need know""".split())
_KB_ALIAS = {"isis": "isi", "institute": "institut", "license": "licence", "licenses": "licence",
             "bachelor": "licence", "bachelors": "licence", "bachelor's": "licence", "msc": "master",
             "masters": "master", "engineers": "engineer", "engineering": "engineer", "ingenieur": "engineer",
             "ingenieurs": "engineer", "iot": "iot", "phd": "doctorate", "doctoral": "doctorate",
             "teachers": "teacher", "professors": "professor", "uni": "university", "admitted": "admit",
             "admitting": "admit", "accepted": "admit", "labs": "lab", "laboratories": "laboratory"}
_KB_CTX = {"isi", "isis", "institute", "institut", "university", "school", "college", "campus", "your", "ariana",
           "utm", "manar", "faculty", "department", "departments"}
_KB_SPECIFIC = {"glsi", "idl", "idisc", "iseoc", "ssii", "mp2l", "dlnt", "siiot", "siiva", "seci", "bioxplore",
                "limtic", "enactus", "securines", "freeways", "villetaneuse", "sorbonne", "galilee", "polytech",
                "saclay", "4c", "prepa", "preparatory", "cycle", "laboratory", "lab", "geii", "gtr", "concours", "pfe",
                "lpic", "ccna", "ieee", "jci", "codex", "artbo", "geeks"}
_KB_PAIRS = [{"research", "master"}, {"professional", "master"}, {"engineer", "specialty"},
             {"engineer", "cycle"}, {"double", "diploma"}, {"student", "club"}]
_KB_DOMAIN = {"student", "admission", "admit", "apply", "application", "club", "internship", "stage", "licence",
              "master", "department", "degree", "program", "diploma", "tuition", "campus", "teacher", "professor"}
_KB_K1, _KB_B = 1.4, 0.6
_KB_YOU = {"you", "you're", "youre", "your", "yours", "yourself"}
_KB_SELF = re.compile(r"\b(how (do|does|did) (you|this)|what are you|who (made|built|created|designed) you|about yourself)\b")


def _kb_tokens(text):
    text = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    out = []
    for w in re.findall(r"[a-z0-9]+", text):
        w = _KB_ALIAS.get(w, w)
        if w in _KB_STOP:
            continue
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(_KB_ALIAS.get(w, w))
    return out


class KnowledgeBase:
    """Loads isi_knowledge.txt and finds the entries that match a question (BM25-style scoring)."""

    def __init__(self, path):
        self.chunks, self.df, self.avg_len, self.enabled = [], Counter(), 1.0, False
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            print(f"WARNING: knowledge file not found ({path}) - school questions are disabled.", flush=True)
            return
        for block in re.split(r"(?m)^## ", raw)[1:]:
            lines = block.strip().splitlines()
            title, kw, body = lines[0].strip(), "", []
            for ln in lines[1:]:
                if ln.lower().startswith("keywords:") and not kw:
                    kw = ln.split(":", 1)[1]
                elif ln.strip():
                    body.append(ln.strip())
            body = " ".join(body)
            tf = Counter()
            for text, weight in ((title, 3.0), (kw, 3.0), (body, 1.0)):
                for t in _kb_tokens(text):
                    tf[t] += weight
            self.chunks.append({"title": title, "body": body, "tf": tf, "len": sum(tf.values())})
        for c in self.chunks:
            self.df.update(c["tf"].keys())
        if self.chunks:
            self.avg_len = sum(c["len"] for c in self.chunks) / len(self.chunks)
            self.enabled = True

    def _score(self, chunk, q):
        n, s = len(self.chunks), 0.0
        for t in q:
            f = chunk["tf"].get(t, 0)
            if f:
                idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                s += idf * f * (_KB_K1 + 1) / (f + _KB_K1 * (1 - _KB_B + _KB_B * chunk["len"] / self.avg_len))
        return s

    def search(self, question, followup=False):
        """Returns the matching entries (possibly none). General questions return []."""
        if not self.enabled:
            return []
        q = set(_kb_tokens(question))
        words = set(re.findall(r"[a-z0-9']+", question.lower()))
        if not q:
            return []
        scored = sorted(((self._score(c, q), c) for c in self.chunks), key=lambda x: -x[0])
        best = scored[0][0]
        about_school = (followup or bool(words & _KB_CTX) or bool(q & _KB_SPECIFIC)
                        or bool(_KB_SELF.search(question.lower())) or any(p <= q for p in _KB_PAIRS)
                        or (bool(words & _KB_YOU) and bool(q & _KB_DOMAIN)))     # "which masters do you offer?"
        ok = best >= 1.0 and about_school
        if not ok:
            if words & {"isi", "isis", "institute", "institut"}:         # asked about ISI but no match
                return [c for c in self.chunks if c["title"].startswith(("About ISI", "Location and contact",
                                                                         "Information I do not have"))]
            return []
        picked, total = [], 0
        for s, c in scored[:KB_MAX_CHUNKS]:
            if s < max(0.8, 0.45 * best) or total + len(c["body"]) > KB_MAX_CHARS and picked:
                continue
            picked.append(c)
            total += len(c["body"])
        return picked


KB = KnowledgeBase(KNOWLEDGE_FILE)
_isi_recent = 0                      # >0 while the conversation is about ISI (so follow-ups still work)


def build_messages(history):
    """System prompt + trimmed history + the new question, with matching ISI facts attached to the question.
    Facts go into the LAST message only, so the earlier part stays identical and Ollama can reuse its cache."""
    global _isi_recent
    question = history[-1]["content"]
    entries = KB.search(question, followup=_isi_recent > 0)
    _isi_recent = 2 if entries else max(0, _isi_recent - 1)
    if entries:
        print("[ISI knowledge used: " + "; ".join(c["title"] for c in entries) + "]", flush=True)

    past, total = [], 0
    for m in reversed(history[:-1][-2 * MAX_HISTORY_TURNS:]):          # newest first, trim to a budget
        content = m["content"]
        if m["role"] == "assistant" and len(content) > HIST_REPLY_CHARS:
            content = content[:HIST_REPLY_CHARS].rsplit(" ", 1)[0] + " ..."
        total += len(content)
        if total > HIST_MAX_CHARS:
            break
        past.append({"role": m["role"], "content": content})
    past.reverse()
    while past and past[0]["role"] != "user":
        past.pop(0)

    if entries:
        facts = "\n".join(f"- {c['body']}" for c in entries)
        question = ("Facts about ISI (use them only if the question is about ISI, and never invent other details):\n"
                    f"{facts}\n\nQuestion: {question}")
    return [{"role": "system", "content": SYSTEM_PROMPT}] + past + [{"role": "user", "content": question}]


PRONOUNCE = [(re.compile(p), r) for p, r in [
    (r"\bISI\b", "I S I"), (r"\bUTM\b", "U T M"), (r"\bGLSI\b", "G L S I"), (r"\bIDL\b", "I D L"),
    (r"\bIDISC\b", "I D I S C"), (r"\bISEOC\b", "I S E O C"), (r"\bSSII\b", "S S I I"),
    (r"\bMP2L\b", "M P 2 L"), (r"\bDLNT\b", "D L N T"), (r"\bSIIo?T\b", "S I I O T"),
    (r"\bSIIVA\b", "S I I V A"), (r"\bSECI\b", "S E C I"), (r"\bGEII\b", "G E I I"), (r"\bGTR\b", "G T R"),
    (r"\bLIMTIC\b", "lim tick"), (r"\b4C\b", "four C"), (r"\bFAD\b", "F A D"), (r"\bPFE\b", "P F E"),
    (r"\bLMD\b", "L M D"), (r"\bCTI\b", "C T I"), (r"\bUVT\b", "U V T"), (r"\bAUF\b", "A U F"),
    (r"\bIFIC\b", "I F I C"), (r"\bEOT\b", "E O T"), (r"\bIEEE\b", "I triple E"), (r"\bIoT\b", "I O T"),
    (r"\bEUR-ACE\b", "Euro ace"), (r"\bENIT\b", "E N I T"), (r"\bCNAM\b", "C N A M"), (r"\bJCI\b", "J C I"),
    (r"\bIUT\b", "I U T"), (r"\bDNISAT\b", "D N I S A T"), (r"\bCCNA\b", "C C N A"), (r"\bLPIC\b", "L P I C"),
    (r"\bDevSecOps\b", "Dev Sec Ops"), (r"\bBac\+(\d)\b", r"Bac plus \1"), (r"\bR&D\b", "R and D")]]


def pronounce(text):
    """Respell acronyms so the English voice reads them letter by letter (the screen keeps the original)."""
    for pattern, repl in PRONOUNCE:
        text = pattern.sub(repl, text)
    return text


def synthesize(text):
    """gTTS -> mp3 -> raw 16-bit mono PCM at 24 kHz. Decoded here so playback never waits on it."""
    fd, path = tempfile.mkstemp(suffix=".mp3", dir=TMP_DIR)
    os.close(fd)
    try:
        gTTS(text=text, lang="en", tld="com", slow=False).save(path)
        out = subprocess.run(["mpg123", "-q", "-s", "-m", "-r", str(TTS_RATE), path],
                             capture_output=True, timeout=30)
        if out.returncode != 0 or len(out.stdout) < 1000:
            raise RuntimeError("could not decode the TTS audio")
        return out.stdout
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


class Speaker:
    """ONE always-open ALSA stream. Silence is fed whenever there is nothing to say, so the USB
    speaker never powers down/up between sentences (that wake-up is what clips the first words)."""
    CHUNK_BYTES = 2048                              # ~43 ms of 24 kHz mono 16-bit

    def __init__(self):
        self.q = queue.Queue()
        self.proc = None
        self._warned = False
        threading.Thread(target=self._run, daemon=True).start()

    def _start(self):
        global current_player
        self.proc = subprocess.Popen(
            ["aplay", "-q", "-D", find_speaker(), "-t", "raw", "-f", "S16_LE",
             "-r", str(TTS_RATE), "-c", "1", f"--buffer-time={AUDIO_BUFFER_US}"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        current_player = self.proc
        try:                                          # small pipe = low delay when interrupting
            fcntl.fcntl(self.proc.stdin.fileno(), getattr(fcntl, "F_SETPIPE_SZ", 1031), PIPE_BYTES)
        except Exception:
            pass

    def _run(self):
        silence = bytes(self.CHUNK_BYTES)
        while True:
            try:
                if self.proc is None or self.proc.poll() is not None:
                    self._start()                     # also re-finds the speaker after a re-plug
                try:
                    chunk = self.q.get_nowait()
                except queue.Empty:
                    chunk = silence
                self.proc.stdin.write(chunk)
                self.proc.stdin.flush()
            except FileNotFoundError:
                if not self._warned:
                    print("ERROR: 'aplay' not found. Install it:  sudo apt install alsa-utils", flush=True)
                    self._warned = True
                time.sleep(2)
            except (BrokenPipeError, OSError, ValueError):
                self.proc = None
                time.sleep(0.5)

    def play(self, pcm):
        n = self.CHUNK_BYTES
        for i in range(0, len(pcm), n):
            piece = pcm[i:i + n]
            self.q.put(piece + bytes(n - len(piece)))

    def busy(self):
        return not self.q.empty()

    def flush(self):
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass


speaker = None


def _put(audio_q, item, stop_event):
    """put() that gives up when the turn was interrupted (so the worker can never get stuck)."""
    while not stop_event.is_set():
        try:
            audio_q.put(item, timeout=0.1)
            return True
        except queue.Full:
            continue
    return False


def tts_worker(messages, audio_q, stop_event, result):
    """Thread: LLM -> sentence -> speech audio. Synthesis of sentence N+1 overlaps playback of N."""
    spoken = []
    try:
        t_prev = time.time()
        for sent in sentences_from_stream(stream_llm(messages), stop_event):
            llm_wait = time.time() - t_prev               # how long we waited for the LLM
            if stop_event.is_set():
                break
            clean = clean_for_speech(sent)
            if not clean:
                t_prev = time.time()
                continue
            print(f"Assistant: {clean}", flush=True)
            t0, pcm = time.time(), None
            for attempt in (1, 2):                        # one retry for flaky internet
                try:
                    pcm = synthesize(pronounce(clean))
                    break
                except Exception as e:
                    print(f"TTS error (try {attempt}): {e}", flush=True)
            t_prev = time.time()
            if pcm is None:
                continue
            stats = {"llm_wait": llm_wait, "tts": time.time() - t0}
            if not _put(audio_q, (clean, pcm, stats), stop_event):
                break
            spoken.append(clean)
    except Exception as e:
        print(f"LLM error: {e}", flush=True)
        result["error"] = str(e)
    finally:
        result["text"] = " ".join(spoken)
        try:
            audio_q.put_nowait(None)
        except queue.Full:
            pass


def play_answer(audio_q, stop_event):
    """Plays sentences as they become ready. Pressing the PTT key interrupts (barge-in).
    Returns True if interrupted."""
    interrupted, done, ready, t_first = False, False, [], None

    # Phase 1: wait until a couple of sentences are ready, so playback does not stutter
    while not done:
        if ptt_down.is_set():
            interrupted = True
            break
        try:
            item = audio_q.get(timeout=0.05)
        except queue.Empty:
            if t_first and time.time() - t_first > PREBUFFER_MAX_WAIT:
                break
            continue
        if item is None:
            done = True
            break
        ready.append(item)
        t_first = t_first or time.time()
        if len(ready) >= PREBUFFER_SENTENCES:
            break

    # Phase 2: play
    idle_since, first = time.time(), True
    while not interrupted:
        if not ready:
            if done:
                break
            try:
                item = audio_q.get(timeout=0.05)
            except queue.Empty:
                if ptt_down.is_set():
                    interrupted = True
                continue
            if item is None:
                done = True
                continue
            ready.append(item)
            continue

        text, pcm, stats = ready.pop(0)
        gap = time.time() - idle_since
        if not first and gap > 0.4:
            print(f"[pause {gap:.1f}s] waited for the next sentence "
                  f"(LLM {stats['llm_wait']:.1f}s, speech synthesis {stats['tts']:.1f}s)", flush=True)
        first = False

        seconds = len(pcm) / (TTS_RATE * 2)
        oled.set_state("TALK", text, duration=max(1.0, seconds))
        speaker.play(pcm)
        deadline = time.time() + seconds + 5
        while speaker.busy():
            if ptt_down.is_set():
                interrupted = True
                break
            if time.time() > deadline:
                print("WARNING: the speaker is not accepting audio (is aplay / the speaker OK?)", flush=True)
                break
            time.sleep(0.03)
        idle_since = time.time()

    if interrupted:
        speaker.flush()
        stop_event.set()
    return interrupted


def speak_text(text):
    """Speak a fixed phrase (used for greetings / confirmations)."""
    try:
        pcm = synthesize(pronounce(text))
    except Exception as e:
        print(f"TTS error: {e}", flush=True)
        return
    q = queue.Queue()
    q.put((text, pcm, {"llm_wait": 0.0, "tts": 0.0}))
    q.put(None)
    play_answer(q, threading.Event())


# =============================================================================
# 7. MAIN LOOP
# =============================================================================
RESET_RE = re.compile(r"\b(reset|clear|forget)\b.*\b(conversation|memory|everything)\b|\bnew conversation\b", re.I)


def main():
    global speaker
    for tool in ("mpg123", "aplay"):
        if not shutil.which(tool):
            print(f"WARNING: '{tool}' is not installed (sudo apt install mpg123 alsa-utils)", flush=True)
    start_mic()
    print(f"-> Speaker: {find_speaker()}", flush=True)
    print(f"-> ISI knowledge: {len(KB.chunks)} entries loaded" if KB.enabled else "-> ISI knowledge: OFF", flush=True)
    speaker = Speaker()
    history = []

    oled.set_state("IDLE")
    speak_text("Assistant online. Hold the E key and speak.")
    print(f"\n=== Ready. Hold [{PTT_KEY.upper()}] to talk. Press it while I'm speaking to interrupt. ===", flush=True)

    while True:
        try:
            oled.set_state("IDLE")
            flush_typed_keys()          # discard anything typed on other keys

            # 1) Record
            raw = record_ptt()

            # 2) Clean audio + transcribe
            oled.set_state("THINK", "Transcribing...")
            wav_path, duration = prepare_audio(raw)
            if not wav_path:
                print("Too short or too quiet - try again.", flush=True)
                oled.set_state("ERROR", "Didn't catch that")
                time.sleep(1.2)
                continue

            user_text = transcribe(wav_path, duration)
            if len(user_text) < 2:
                print("No speech recognised.", flush=True)
                oled.set_state("ERROR", "No speech heard")
                time.sleep(1.2)
                continue
            print(f"You: {user_text}", flush=True)

            # Voice command: reset memory
            if RESET_RE.search(user_text):
                history.clear()
                speak_text("Okay, starting a fresh conversation.")
                continue

            # 3) LLM + TTS pipeline (streams sentence by sentence)
            history.append({"role": "user", "content": user_text})
            messages = build_messages(history)
            oled.set_state("THINK", f"You: {user_text}")

            stop_event, audio_q, result = threading.Event(), queue.Queue(maxsize=3), {}
            worker = threading.Thread(target=tts_worker,
                                      args=(messages, audio_q, stop_event, result), daemon=True)
            worker.start()
            interrupted = play_answer(audio_q, stop_event)
            worker.join(timeout=0.5 if interrupted else 10)

            reply = result.get("text", "")
            if reply:
                history.append({"role": "assistant", "content": reply})
            else:
                history.pop()
                if result.get("error"):
                    oled.set_state("ERROR", "LLM unavailable")
                    time.sleep(1.5)
            del history[:-2 * MAX_HISTORY_TURNS]

            if interrupted:
                print("[Interrupted]", flush=True)

        except Exception as e:
            print(f"\nLoop error: {e}", flush=True)
            oled.set_state("ERROR", "Error - retrying")
            time.sleep(1.0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        shutdown()
