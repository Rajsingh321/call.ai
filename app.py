import sys
import os
import json
from datetime import datetime, timedelta
import requests
from flask import Flask, request, Response, jsonify, send_file, url_for
from twilio.twiml.voice_response import VoiceResponse, Dial
import speech_recognition as sr
from dotenv import load_dotenv

# Optional: Groq LLM
try:
    from groq import Groq
except:
    Groq = None

# ========================================
# LOAD ENV
# ========================================
load_dotenv()

LLM_API_KEY = os.getenv("LLM_API_KEY")

TW_SID = os.getenv("TWILIO_ACCOUNT_SID")
TW_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TW_FROM = os.getenv("TWILIO_NUMBER")       # Twilio From Number
AI_NUMBER = os.getenv("TWILIO_AI_NUMBER")  # AI Twilio Number
BASE_URL = os.getenv("BASE_URL")           # ngrok URL
FLASK_BASE = os.getenv("FLASK_BASE", "http://localhost:5000")

STATE_FILE = "state.json"
AUDIO_DIR = "/mnt/data"

# ========================================
# Create Flask app
# ========================================
app = Flask(__name__)

# ========================================
# UTIL FUNCTIONS (Shared for backend + UI)
# ========================================
def load_state():
    if not os.path.exists(STATE_FILE):
        data = {
            "mode": "normal",
            "reason": "",
            "active": False,
            "expires": None,
            "user_number": None,
        }
        save_state(data)
        return data
    return json.load(open(STATE_FILE))

def save_state(data):
    json.dump(data, open(STATE_FILE, "w"))

def is_mode_active():
    st = load_state()
    if not st["active"]:
        return False

    if not st["expires"]:
        return False

    exp = datetime.fromisoformat(st["expires"])
    if datetime.utcnow() > exp:
        st["active"] = False
        st["expires"] = None
        save_state(st)
        return False

    return True

# ========================================
# MODE ENDPOINTS (used by Streamlit UI)
# ========================================
@app.route("/set-mode", methods=["POST"])
def set_mode():
    data = request.get_json()
    mode = data.get("mode")
    reason = data.get("reason", "")
    duration = int(data.get("duration", 5))
    if duration < 1: duration = 1
    if duration > 60: duration = 60

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
    return jsonify({"status": "ok"})

@app.route("/status", methods=["GET"])
def status():
    state = load_state()
    state["active"] = is_mode_active()
    save_state(state)
    return jsonify(state)

# ========================================
# TEST AUDIO ROUTES
# ========================================
@app.route("/audio/<name>")
def audio(name):
    path = os.path.join(AUDIO_DIR, name)
    if not os.path.exists(path):
        return "Audio not found", 404
    return send_file(path)

@app.route("/play-audio")
def play_audio():
    file = request.args.get("file", "test_caller_urgent.wav")
    public = url_for("audio", name=file, _external=True)
    r = VoiceResponse()
    r.play(public)
    r.hangup()
    return Response(str(r), mimetype="text/xml")

# ========================================
# TWILIO INBOUND CALL
# ========================================
@app.route("/incoming-call", methods=["POST"])
def incoming_call():
    r = VoiceResponse()

    if is_mode_active():
        st = load_state()
        r.say(f"The user is currently in {st['mode']} mode. Please speak after the beep.")
    else:
        r.say("Hello! I am your AI assistant. Please speak after the beep.")

    r.record(action="/process-recording", method="POST", play_beep=True)
    return Response(str(r), mimetype="text/xml")

# ========================================
# PROCESS RECORDING (AI LOGIC)
# ========================================
@app.route("/process-recording", methods=["POST"])
def process_recording():
    st = load_state()

    url = request.form.get("RecordingUrl") + ".wav"
    audio_file = "caller.wav"

    try:
        data = requests.get(url).content
        open(audio_file, "wb").write(data)
    except:
        r = VoiceResponse()
        r.say("Sorry, error processing audio.")
        r.hangup()
        return Response(str(r), mimetype="text/xml")

    # STT
    rec = sr.Recognizer()
    with sr.AudioFile(audio_file) as src:
        audio = rec.record(src)
    try:
        text = rec.recognize_google(audio)
    except:
        text = ""

    # Urgency Check
    urgent = check_urgent(text)

    r = VoiceResponse()

    if urgent and st["user_number"]:
        r.say("This seems urgent. Connecting you now.")
        d = Dial()
        d.number(st["user_number"])
        r.append(d)
        return Response(str(r), mimetype="text/xml")

    r.say(mode_reply(st))
    r.hangup()
    return Response(str(r), mimetype="text/xml")

# ========================================
# URGENT DETECTOR
# ========================================
def check_urgent(text):
    text = (text or "").lower()
    urgent_words = ["urgent", "important", "emergency", "immediately", "help"]
    return any(w in text for w in urgent_words)

# ========================================
# MODE REPLY
# ========================================
def mode_reply(st):
    mode = st["mode"]
    reason = st["reason"]

    if mode == "sleep":
        return "The user is sleeping. I will notify them."
    if mode == "meeting":
        return "The user is in a meeting."
    if mode == "driving":
        return "The user is driving."
    if mode == "custom":
        return f"The user is unavailable: {reason}"

    return "The user is not available."

# ========================================
# STREAMLIT FRONTEND
# ========================================
def run_ui():
    import streamlit as st
    import requests

    st.set_page_config(page_title="Call.AI", layout="centered")
    st.title("📞 Call.AI Mobile App")

    menu = st.sidebar.selectbox("Menu", ["Welcome","Enter Number","Modes","Forwarding","Test Call"])

    if menu == "Welcome":
        st.header("Welcome to Call.AI")
        st.write("This is your mobile-style UI for managing call forwarding + modes.")

    if menu == "Enter Number":
        st.header("Enter your phone number")
        num = st.text_input("Phone Number (+91...)")
        if st.button("Save"):
            requests.post(f"{FLASK_BASE}/set-mode", json={
                "mode":"normal","reason":"","duration":1,"user_number":num
            })
            requests.post(f"{FLASK_BASE}/clear-mode")
            st.success("Saved!")

    if menu == "Modes":
        st.header("Select Mode")
        mode = st.selectbox("Mode", ["sleep","meeting","driving","custom"])
        reason = ""
        if mode == "custom":
            reason = st.text_input("Reason")
        dur = st.slider("Duration (min)", 1, 60, 10)
        if st.button("Activate"):
            requests.post(f"{FLASK_BASE}/set-mode", json={
                "mode":mode,"reason":reason,"duration":dur,
                "user_number":load_state()["user_number"]
            })
            st.success("Mode ON")

        if st.button("Clear"):
            requests.post(f"{FLASK_BASE}/clear-mode")
            st.success("Cleared")

        st.json(requests.get(f"{FLASK_BASE}/status").json())

    if menu == "Forwarding":
        st.header("Activate Call Forwarding")
        if AI_NUMBER:
            code = f"**61*{AI_NUMBER}#"
            st.write("Tap this button on mobile:")
            st.markdown(f"[Open Dialer → {code}](tel:{code})")
        else:
            st.error("AI_NUMBER missing")

    if menu == "Test Call":
        st.header("Test AI Flow")
        from twilio.rest import Client
        client = Client(TW_SID, TW_TOKEN)

        opt = st.selectbox("Caller Type", ["urgent audio","not urgent audio"])
        file = "test_caller_urgent.wav" if opt=="urgent audio" else "test_caller_noturgent.wav"
        if st.button("Start Test Call"):
            url = f"{BASE_URL}/play-audio?file={file}"
            call = client.calls.create(to=AI_NUMBER, from_=TW_FROM, url=url)
            st.success(f"Call SID: {call.sid}")

# ========================================
# ENTRY POINT
# ========================================
if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "ui":
            run_ui()
        else:
            print("Unknown command.")
    else:
        print("Running backend... Use:")
        print("  python app.py ui    -> Streamlit UI")
        print("  python app.py       -> Backend")
        app.run(port=5000)


