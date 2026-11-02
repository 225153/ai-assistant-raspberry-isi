# ISI Voice Assistant

A push-to-talk voice assistant that runs on a **Raspberry Pi**. You hold a key, speak, and it answers out loud while a small **OLED screen** shows an animated face and the text of the answer.

It answers general questions like any chatbot, and it also knows about the **Institut Supérieur d'Informatique (ISI)**, Tunisia (programs, departments, clubs, contact details and more) thanks to a small knowledge file you can edit.

## Features

- **Push-to-talk:** hold **E**, speak (up to 5 minutes), release. Press **E** again while it talks to interrupt it.
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

## Installation, step by step

### 1. Update the system and install system packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-venv python3-pip python3-dev build-essential git \
                    mpg123 alsa-utils i2c-tools fonts-dejavu-core
```

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

This installs: `numpy`, `Pillow` (drawing on the OLED), `smbus2` (talking to the OLED), `groq` (speech recognition and optional cloud LLM), `gTTS` (text to speech), `ollama` (local LLM client) and `evdev` (reading the push-to-talk key).

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
| Hold **E**, speak, release | Ask a question |
| Press **E** while it is speaking | Interrupts the answer and starts listening |
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
| `KEY_DEBUG=1` | Print every E key press/release (to debug the keyboard) |

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
