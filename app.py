# app.py
from flask import Flask, request, Response, jsonify, send_file, url_for
from twilio.twiml.voice_response import VoiceResponse, Dial
import requests
import speech_recognition as sr
import os
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
from groq import Groq  # keep if you have groq; fallback will use keywords

load_dotenv()

app = Flask(__name__)

# === ENV VARIABLES ===
LLM_API_KEY = os.getenv("LLM_API_KEY")    # optional: Groq key
STATE_FILE = "state.json"
AUDIO_DIR = "/mnt/data"  # used for test audio files (adjust if needed)


# ===========================
# UTILS
# ===========================
def load_state():
    if not os.path.exists(STATE_FILE):
        default = {
            "mode": "normal",
            "reason": "",
            "active": False,
            "expires": None,
            "user_number": None,
        }
        save_state(default)
        return default
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def is_mode_active():
    state = load_state()
    if not state.get("active"):
        return False

    expires = state.get("expires")
    if not expires:
        return False

    try:
        expire_time = datetime.fromisoformat(expires)
    except Exception:
        # invalid format — clear mode
        state["active"] = False
        save_state(state)
        return False

    if datetime.utcnow() > expire_time:
        # mode expired
        state["active"] = False
        state["expires"] = None
        save_state(state)
        return False

    return True


# ===========================
# APP (MOBILE) CONTROL ENDPOINTS
# ===========================
@app.route("/set-mode", methods=["POST"])
def set_mode():
    """
    JSON body:
    {
      "mode": "sleep" | "meeting" | "driving" | "custom",
      "reason": "optional string",
      "duration": minutes (int),
      "user_number": "+91..."
    }
    """
    data = request.get_json(force=True)
    mode = data.get("mode", "normal")
    reason = data.get("reason", "")
    try:
        duration = int(data.get("duration", 5))
        if duration < 1:
            duration = 1
        if duration > 60:
            duration = 60
    except:
        duration = 5
    user_number = data.get("user_number")

    state = {
        "mode": mode,
        "reason": reason,
        "active": True,
        "expires": (datetime.utcnow() + timedelta(minutes=duration)).isoformat(),
        "user_number": user_number
    }
    save_state(state)
    return jsonify({"status": "ok", "state": state})


@app.route("/clear-mode", methods=["POST"])
def clear_mode():
    state = load_state()
    state["active"] = False
    state["mode"] = "normal"
    state["reason"] = ""
    state["expires"] = None
    save_state(state)
    return jsonify({"status": "ok", "state": state})


@app.route("/status", methods=["GET"])
def status():
    state = load_state()
    # refresh active flag based on time
    state["active"] = is_mode_active()
    save_state(state)
    return jsonify(state)


# =================================================
# TEST AUDIO SERVING & PLAY TWIML (for desktop testing)
# =================================================
@app.route("/audio/<name>", methods=["GET"])
def serve_audio(name):
    # serve an audio file from AUDIO_DIR, e.g. /mnt/data/test_caller_urgent.wav
    safe_path = os.path.join(AUDIO_DIR, name)
    if not os.path.isfile(safe_path):
        return ("Audio not found: " + safe_path, 404)
    return send_file(safe_path, mimetype="audio/wav")


@app.route("/play-audio", methods=["GET", "POST"])
def play_audio_twiML():
    """
    Twilio will request this TwiML when making an outbound test call.
    Provide query param ?file=test_caller_urgent.wav
    """
    filename = request.args.get("file", "test_caller_urgent.wav")
    public_audio_url = url_for("serve_audio", name=filename, _external=True)

    resp = VoiceResponse()
    resp.play(public_audio_url)
    resp.hangup()
    return Response(str(resp), mimetype="text/xml")


# =================================================
# INCOMING CALL HANDLER
# =================================================
@app.route("/incoming-call", methods=["POST"])
def incoming_call():
    """
    Twilio will POST here on inbound call.
    If mode active: custom greeting about mode.
    Then record and send to /process-recording.
    """
    response = VoiceResponse()

    if is_mode_active():
        state = load_state()
        mode = state.get("mode", "unavailable")
        msg = f"The user is currently in {mode} mode. Please speak after the beep."
    else:
        msg = "Hello! I am your AI assistant. Please speak after the beep."

    response.say(msg)
    response.record(action="/process-recording", method="POST", play_beep=True, timeout=60, max_length=120)
    # If recording doesn't happen, you may branch, but this is simple flow.
    return Response(str(response), mimetype="text/xml")


# =================================================
# PROCESS RECORDING -> STT -> LLM -> REPLY / FORWARD
# =================================================
@app.route("/process-recording", methods=["POST"])
def process_recording():
    state = load_state()
    recording_url = request.form.get("RecordingUrl")
    if not recording_url:
        # no recording — politely hang up
        resp = VoiceResponse()
        resp.say("Sorry, I did not get that. Goodbye.")
        resp.hangup()
        return Response(str(resp), mimetype="text/xml")

    # Twilio gives a recording URL without extension; we request .wav
    audio_url = recording_url + ".wav"
    audio_file = "caller.wav"
    try:
        audio_data = requests.get(audio_url, timeout=15).content
        with open(audio_file, "wb") as f:
            f.write(audio_data)
    except Exception as e:
        print("Failed to download recording:", e)
        resp = VoiceResponse()
        resp.say("Sorry, there was an error processing your message.")
        resp.hangup()
        return Response(str(resp), mimetype="text/xml")

    # Speech-to-text (using google via SpeechRecognition wrapper)
    recognizer = sr.Recognizer()
    with sr.AudioFile(audio_file) as source:
        audio = recognizer.record(source)

    try:
        caller_text = recognizer.recognize_google(audio)
    except Exception as e:
        print("STT error:", e)
        caller_text = ""

    # Decide urgency
    urgent = is_urgent(caller_text)

    # If urgent and we have a user number, forward the call immediately
    if urgent and state.get("user_number"):
        resp = VoiceResponse()
        resp.say("This seems urgent. I will connect you to the user now. Please hold.")
        d = Dial()
        d.number(state["user_number"])
        resp.append(d)
        return Response(str(resp), mimetype="text/xml")

    # Not urgent: speak a mode-based reply (short) and hang up
    reply = mode_reply(state, caller_text)
    resp = VoiceResponse()
    resp.say(reply)
    resp.hangup()
    return Response(str(resp), mimetype="text/xml")


# =================================================
# URGENCY DETECTION using Groq or keyword fallback
# =================================================
def is_urgent(text):
    text = (text or "").strip()
    if not text:
        return False
    # Try Groq if API key present
    if LLM_API_KEY:
        try:
            client = Groq(api_key=LLM_API_KEY)
            prompt = f"Decide if this message is urgent. Reply YES or NO only.\nMessage: \"{text}\""
            resp = client.chat.completions.create(
                model="llama3-8b-8192",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=3,
            )
            ans = resp.choices[0].message["content"].strip().upper()
            return "YES" in ans
        except Exception as e:
            print("Groq error (falling back):", e)

    # keyword fallback
    urgent_words = ["urgent", "emergency", "important", "asap", "immediately", "help"]
    t = text.lower()
    return any(w in t for w in urgent_words)


# =================================================
# MODE-BASED REPLY (short)
# =================================================
def mode_reply(state, text):
    mode = state.get("mode", "normal")
    reason = state.get("reason", "") or ""

    if mode == "sleep":
        return "The user is currently sleeping. I will notify them."
    if mode == "meeting":
        return "The user is in a meeting and cannot take calls right now."
    if mode == "driving":
        return "The user is driving. I will tell them to call you when safe."
    if mode == "custom":
        if reason:
            return f"The user is not available: {reason}. I will let them know."
        return "The user is currently unavailable. I will notify them."
    # normal fallback
    return "The user is not available right now. They will call you back soon."

# =================================================
# RUN SERVER
# =================================================
if __name__ == "__main__":
    # ensure state file exists
    load_state()
    app.run(host="0.0.0.0", port=5000)
