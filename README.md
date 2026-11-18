# ISI Voice Assistant

A push-to-talk voice assistant that runs on a **Raspberry Pi**. You hold a key, speak, and it answers out loud while a small **OLED screen** shows an animated face and the text of the answer.

It answers general questions like any chatbot, and it also knows about the **Institut Supérieur d'Informatique (ISI)**, Tunisia (programs, departments, clubs, contact details and more) thanks to a small knowledge file you can edit.

## Features

- **Push-to-talk:** hold **E** on the keyboard **or** a physical push button on GPIO 17, speak (up to 5 minutes), release. Press again while it talks to interrupt it.
- **Accurate speech recognition:** Whisper large-v3 (through Groq), forced to English, with audio cleanup.
- **Streaming answers:** the answer is spoken sentence by sentence while it is still being written.
- **Conversation memory:** remembers the last 6 exchanges. Say "new conversation" to reset it.
- **Animated OLED:** blinking face, live microphone level meter, "thinking" animation, scrolling text.
- **ISI knowledge base:** a plain text file (`isi_knowledge.txt`); only the relevant entries are sent to the model, so it stays fast.
- **Plug-and-play USB audio:** the microphone and speaker are found by name, so the USB port does not matter. A stalled microphone is restarted automatically.
- **Runs the "brain" locally** (Gemma 3 1B through Ollama) or in the cloud (Llama 3.3 70B through Groq).

## How it works

```
 Microphone ──► arecord ──► clean-up (trim silence, normalise)
                                   │
                                   ▼
                      Groq Whisper (speech → text)
                                   │
          isi_knowledge.txt ──► find relevant ISI facts (if the question is about ISI)
                                   │
                                   ▼
                    LLM: Gemma 3 1B (Ollama, local)  or  Llama 3.3 (Groq, cloud)
                                   │   (streams sentence by sentence)
                                   ▼
                       gTTS (text → speech) ──► aplay ──► USB speaker
                                   
 OLED screen: animated face + scrolling text, driven by the program's state
```

## What you need

**Hardware**

| Item | Notes |
|---|---|
| Raspberry Pi 4 or 5 | 4 GB of RAM or more is recommended |
| microSD card with Raspberry Pi OS | 64-bit, Bookworm or newer |
| OLED display 128x64, SH1106, I2C | address `0x3C` |
| USB microphone / USB sound card | tested with a Texas Instruments PCM2902 |
| USB speaker | tested with a Jieli UACDemoV1.0 |
| USB keyboard | for the push-to-talk key |
| Push button (optional) | any normally-open momentary push button + 2 jumper wires, see [the button section](#optional-push-to-talk-button) |
| Internet connection | for speech recognition and text to speech |

**Accounts**

- A free **Groq** account and API key: <https://console.groq.com/keys>

### OLED wiring

| OLED pin | Raspberry Pi pin |
|---|---|
| VCC | Pin 1 (3.3 V) |
| GND | Pin 6 (GND) |
| SDA | Pin 3 (GPIO 2 / SDA) |
| SCL | Pin 5 (GPIO 3 / SCL) |

### Optional: push-to-talk button

A physical button works **in addition to** the E key: holding either one records, and either one interrupts the answer. The program reads **GPIO 17** (physical pin 11) directly. The button connects that pin to ground when pressed, and the program starts listening.

**What you need:** one normally-open momentary push button (a 4-leg tactile switch or a 2-wire panel button) and 2 jumper wires (male-to-female if the button is on a breadboard).

**Do I need a resistor? No.** The Pi's built-in pull-up resistor is switched on by the program. An optional protection resistor is described below.

| Button leg | Wire colour (convention) | Raspberry Pi pin |
|---|---|---|
| leg 1 | yellow (or green), the signal | **Pin 11 = GPIO 17** |
| leg 2 | black, ground | **Pin 9 = GND** |

Wire colours do not matter electrically; the convention is black for ground and any other colour for a signal. Avoid red, which usually means power. A button has no polarity. On a 4-leg tactile switch use two **diagonally opposite** legs (legs on the same side are connected inside). On a breadboard, the five holes of one numbered row on one side of the middle channel are linked, so plug each wire into the same row and side as its leg. If the program behaves as if the button is always pressed, turn the button 90 degrees.

```
 Raspberry Pi header (pins 1 to 12, seen with the USB ports at the bottom)

   Pin 1  3.3V    [OLED VCC]   |  Pin 2  5V
   Pin 3  SDA     [OLED SDA]   |  Pin 4  5V
   Pin 5  SCL     [OLED SCL]   |  Pin 6  GND  [OLED GND]
   Pin 7  GPIO 4               |  Pin 8  GPIO 14
   Pin 9  GND     [BUTTON]     |  Pin 10 GPIO 15
   Pin 11 GPIO 17 [BUTTON]     |  Pin 12 GPIO 18
```

**Optional protection resistor:** 330 Ohm to 1 kOhm in series with the yellow wire (330 Ohm bands: orange, orange, brown, gold; 1 kOhm bands: brown, black, red, gold). Not required.

> **Safety:** the button must only connect GPIO 17 to **GND**. Never connect a GPIO pin to the 5V pins (2 and 4): GPIO pins are 3.3 V only.

**Software:** nothing to configure. The needed packages are installed in steps 1 and 5 (`swig`, `liblgpio-dev`, `gpiozero`, `lgpio`). When the program starts you should see:

```
-> Push button ready on GPIO 17: hold it to talk
```

To use another pin run with `BUTTON_GPIO=27` (BCM number), or `BUTTON_GPIO=0` to turn the button off. If the GPIO library cannot be installed, the program still works with the keyboard and prints `Push button: OFF`.

**Alternative without the GPIO library:** add this line at the end of `/boot/firmware/config.txt` and reboot. The Pi then makes the button behave like the E key, and the program reads it like a keyboard (no library needed):

```
dtoverlay=gpio-key,gpio=17,active_low=1,gpio_pull=up,label=ptt,keycode=18
```

Do not use both methods at once.

## Installation, step by step

### 1. Update the system and install system packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-venv python3-pip python3-dev build-essential git \
                    mpg123 alsa-utils i2c-tools fonts-dejavu-core swig liblgpio-dev
```

- `swig` and `liblgpio-dev` are only needed to build the GPIO library for the optional push button.
- `mpg123` decodes the speech audio, `alsa-utils` provides `aplay` and `arecord`, `i2c-tools` is for checking the screen, and `fonts-dejavu-core` gives the OLED a readable font.

### 2. Enable I2C (for the OLED)

```bash
sudo raspi-config nonint do_i2c 0
sudo reboot
```

After the reboot, check that the screen is detected:

```bash
sudo i2cdetect -y 1
```

You should see `3c` in the table. If you don't, check the wiring.

### 3. Get the project

```bash
git clone https://github.com/YOUR_USERNAME/isi-voice-assistant.git
cd isi-voice-assistant
```

(Or copy the project folder to the Pi and `cd` into it.)

### 4. Create and activate a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
```

Your prompt now starts with `(venv)`. Every time you open a new terminal, run `source venv/bin/activate` again from the project folder.

### 5. Install the Python libraries

```bash
pip install -r requirements.txt
```

This installs: `numpy`, `Pillow` (drawing on the OLED), `smbus2` (talking to the OLED), `groq` (speech recognition and optional cloud LLM), `gTTS` (text to speech), `ollama` (local LLM client) and `evdev` (reading the push-to-talk key), and `gpiozero` + `lgpio` (reading the optional push button; the build takes a minute or two).

### 6. Install Ollama and download the local model

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull gemma3:1b
```

Test it:

```bash
ollama run gemma3:1b "Say hello in one sentence"
```

Type `/bye` to leave. If you have a Pi 5 with 8 GB you can try `gemma3:4b` (smarter, slower): `ollama pull gemma3:4b`, then change `OLLAMA_MODEL` in `isiassistant.py`.

### 7. Add your Groq API key (safely)

Create a file called `.env` in the project folder:

```bash
nano .env
```

and put this single line in it (use your own key):

```
GROQ_API_KEY=your_key_here
```

The program loads this file automatically (it must be in the same folder as `isiassistant.py`). Tip: `chmod 600 .env` so only you can read it.

> **Never put the key inside the Python code and never commit `.env`.** It is already listed in `.gitignore`. If a key was ever shown or committed anywhere, revoke it at <https://console.groq.com/keys> and create a new one.

### 8. Check your microphone and speaker

```bash
lsusb          # both USB audio devices should appear
arecord -l     # lists capture devices (the microphone)
aplay -l       # lists playback devices (the speaker)
```

The name inside the brackets (for example `CODEC` or `UACDemoV10`) is the card name. Test them, replacing the names with yours:

```bash
arecord -D plughw:CARD=CODEC,DEV=0 -f S16_LE -r 16000 -c 1 -d 4 /tmp/test.wav
aplay -D plughw:CARD=UACDemoV10,DEV=0 /tmp/test.wav
```

If you hear yourself, the audio hardware works. The program finds the devices by name automatically (see [Configuration](#configuration) if your devices have different names).

### 9. Run it

```bash
sudo ./venv/bin/python isiassistant.py       # the program reads the key from the .env file by itself
```

Why `sudo`? Reading the keyboard directly needs access to `/dev/input`. If you prefer to run **without** sudo, add yourself to the right groups once, then log out and in again:

```bash
sudo usermod -aG input,i2c,audio $USER
```

and then simply run `python isiassistant.py` (with the venv active). If you get a permission error, go back to `sudo`.

At startup you should see lines like:

```
-> Keys: evdev (auto re-scans keyboards)
-> Microphone: plughw:CARD=CODEC,DEV=0
-> Speaker: plughw:CARD=UACDemoV10,DEV=0
-> ISI knowledge: 23 entries loaded
```

## Using it

| Action | What it does |
|---|---|
| Hold **E** (or the button), speak, release | Ask a question |
| Press **E** (or the button) while it is speaking | Interrupts the answer and starts listening |
| Say "new conversation" or "clear the memory" | Resets the conversation memory |
| **Ctrl + C** | Exits cleanly (the OLED is cleared) |

Other keys are ignored. Examples to try:

- "What is the capital of Japan?" (general question)
- "What programs does ISI offer?"
- "Tell me about the engineering cycle." then "And the masters?" (follow-ups work)
- "How can I contact your school?"

## Teaching it about your school

All school knowledge lives in `isi_knowledge.txt`. Each entry looks like this:

```
## Clubs
keywords: club clubs student activities ieee enactus
ISI has eleven clubs: Enactus, Securines, ... (the facts, in short plain sentences)
```

- A line starting with `## ` is the title.
- `keywords:` adds extra words people might say (synonyms help matching).
- The text below it is what the assistant is allowed to use. Keep each entry under about 800 characters.

Restart the program after editing. When a question matches, the terminal prints `[ISI knowledge used: ...]`, which helps you see what it found. Questions that do not match any entry are answered normally by the model.

For information that is not in the file, the assistant is told to say it doesn't have it and suggest contacting the school, instead of guessing.

## Configuration

**Environment variables** (set them before the command, for example `sudo LLM_BACKEND=groq ./venv/bin/python isiassistant.py`, or add a line such as `LLM_BACKEND=groq` to your `.env` file):

| Variable | Meaning |
|---|---|
| `GROQ_API_KEY` | Your Groq key (required) |
| `LLM_BACKEND` | `ollama` (default, local Gemma) or `groq` (cloud Llama, smarter and faster) |
| `MIC_DEVICE` | Force a microphone, e.g. `plughw:1,0` |
| `SPEAKER_DEVICE` | Force a speaker, e.g. `plughw:2,0` |
| `KNOWLEDGE_FILE` | Use a different knowledge file |
| `KEY_DEBUG=1` | Print every E key and button press/release (to debug) |
| `BUTTON_GPIO` | GPIO number of the push button (default `17`; `0` turns the button off) |

**Settings at the top of `isiassistant.py`:**

| Setting | Meaning |
|---|---|
| `OLLAMA_MODEL`, `GROQ_LLM_MODEL` | Which models to use |
| `STT_MODEL` | Speech model (`whisper-large-v3` is the most accurate) |
| `LLM_MAX_TOKENS` | Maximum answer length (about 450 tokens is roughly 2 minutes of speech) |
| `MAX_HISTORY_TURNS` | How many exchanges are remembered |
| `PREBUFFER_SENTENCES` | `2` is smoother, `1` starts speaking sooner |
| `OLLAMA_THREADS` | Set to `3` on a 4-core Pi if the audio crackles while it thinks |
| `MIC_HINTS`, `SPEAKER_HINTS` | Words used to recognise your USB devices by name |
| `PTT_KEY` | The push-to-talk key (default `e`) |

## Free-tier limits (Groq)

With the default setup only speech recognition uses Groq: roughly **2,000 questions per day** and **8 hours of audio per day** on the free tier. If you use `LLM_BACKEND=groq` the answer model also counts, and its daily token limit is much lower (about 100 to 200 thousand tokens per day for the 70B model). Limits change, so check <https://console.groq.com/settings/limits>.

## Start automatically when the Pi boots (optional)

Create `/etc/systemd/system/isi-assistant.service` (replace `YOUR_USER` and the folder):

```ini
[Unit]
Description=ISI Voice Assistant
After=network-online.target ollama.service sound.target
Wants=network-online.target

[Service]
User=root
WorkingDirectory=/home/YOUR_USER/isi-voice-assistant
EnvironmentFile=/home/YOUR_USER/isi-voice-assistant/.env
ExecStart=/home/YOUR_USER/isi-voice-assistant/venv/bin/python isiassistant.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now isi-assistant
journalctl -u isi-assistant -f        # watch the log
```

## Troubleshooting

| Problem | What to try |
|---|---|
| `GROQ_API_KEY not found` | Check that `.env` is in the same folder as `isiassistant.py` and contains `GROQ_API_KEY=your_key` (no spaces around `=`) |
| OLED stays blank | Run `sudo i2cdetect -y 1` (you should see `3c`). Check wiring and that I2C is enabled |
| The push button does nothing | At start-up you must see `-> Push button ready on GPIO 17`. If it says `OFF`, the GPIO library is missing: `pip install gpiozero lgpio` (needs `swig` and `liblgpio-dev`). Then run with `KEY_DEBUG=1`: pressing the button must print `[Button pressed]`. If not, recheck the wiring (pin 11 and pin 9) |
| Nothing happens when I press E | Run with `KEY_DEBUG=1`. If no lines appear, use `sudo -E` or check the keyboard is the one plugged in |
| "No microphone found - plug it in" | Check `arecord -l`. If your mic has an unusual name, set `MIC_DEVICE=plughw:CARD=<name>,DEV=0` |
| "Microphone stalled - restarting it..." often | USB power or bandwidth. Use a **powered USB hub** |
| No sound from the speaker | Check `aplay -l`, test with `aplay`, or set `SPEAKER_DEVICE` |
| Speech is cut off or pauses between sentences | Look for `[pause ...]` lines in the log; the LLM may be slow. Try `LLM_BACKEND=groq`, or `PREBUFFER_SENTENCES = 2` |
| Crackling audio while it thinks | Set `OLLAMA_THREADS = 3` and/or raise `AUDIO_BUFFER_US` to `500000` |
| `LLM error` or connection refused | Ollama is not running: `sudo systemctl start ollama` |
| Wrong words from speech recognition | Speak closer to the microphone; check the mic level with the OLED meter |
| Wrong or invented facts about ISI | Add or fix the entry in `isi_knowledge.txt`, or use `LLM_BACKEND=groq` (follows facts better than the 1B model) |

## Project structure

```
isi-voice-assistant/
├── isiassistant.py       # the main program
├── isi_knowledge.txt     # facts about ISI (edit me)
├── requirements.txt      # Python libraries
├── README.md             # this file
├── .gitignore
└── .env                  # your API key (NOT committed)
```

## Security notes

- Never hard-code the API key and never commit `.env`.
- Before your first `git commit`, check that no key is left in any file:
  `grep -rn "gsk_" . --include="*.py" --exclude-dir=venv`
  (it should print nothing).
- Anything committed stays in the git history. If a key was ever committed, revoke it on the Groq console.

## Credits

Built with [Groq](https://groq.com) (Whisper), [Ollama](https://ollama.com) (Gemma 3), [gTTS](https://gtts.readthedocs.io), [Pillow](https://python-pillow.org) and [python-evdev](https://python-evdev.readthedocs.io). Made for the Institut Supérieur d'Informatique (ISI), Université de Tunis El Manar, Tunisia.
