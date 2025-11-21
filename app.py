from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Dial
import requests
import speech_recognition as sr
import os
import json
from datetime import datetime
from dotenv import load_dotenv
from groq import Groq

load_dotenv()
app = Flask(__name__)

# === ENV VARIABLES ===
LLM_API_KEY = os.getenv("LLM_API_KEY")       
TWILIO_AUTH = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_NUMBER")     # Your AI number

STATE_FILE = "call_state.json"


# -----------------------------
# Helper: Read Mode State
# -----------------------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "mode": "normal",
            "active": False,
            "expires": "",
            "user_number": "",
            "custom_reason": ""
        }
    with open(STATE_FILE, "r") as f:
        return json.load(f)


# -----------------------------
# Helper: Urgency Detection
# -----------------------------
def detect_urgent(text):
    keywords = ["urgent", "important", "emergency", "help", "immediately", "asap"]
    if any(k in text.lower() for k in keywords):
        return True

    # AI-based urgent detection (optional)
    client = Groq(api_key=LLM_API_KEY)
    prompt = f"""
        Caller message: "{text}"
        Decide if this is urgent. Answer strictly YES or NO.
    """
    res = client.chat.completions.create(
        model="llama3-8b-8192",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=5
    )
    out = res.choices[0].message["content"].strip().upper()
    return "YES" in out


# -----------------------------
# 1) INCOMING CALL HANDLER
# -----------------------------
@app.route("/incoming-call", methods=["POST"])
def incoming_call():
    state = load_state()

    # If mode expired → deactivate
    if state["active"]:
        if datetime.utcnow() > datetime.fromisoformat(state["expires"]):
            state["active"] = False
            with open(STATE_FILE, "w") as f:
                json.dump(state, f)

    response = VoiceResponse()

    # Greeting based on mode
    if state["active"]:
        mode = state["mode"]

        if mode == "sleep":
            response.say("User is sleeping right now. Please speak after the beep.")
        elif mode == "meeting":
            response.say("User is in a meeting. Please speak after the beep.")
        elif mode == "driving":
            response.say("User is driving. Please speak after the beep.")
        elif mode == "custom":
            response.say(f"{state['custom_reason']}. Please speak after the beep.")
    else:
        response.say("User is unavailable right now. Please speak after the beep.")

    # Record caller
    response.record(
        action="/process-recording",
        method="POST",
        play_beep=True
    )

    return Response(str(response), mimetype="text/xml")


# -----------------------------
# 2) PROCESS RECORDING
# -----------------------------
@app.route("/process-recording", methods=["POST"])
def process_recording():
    state = load_state()

    recording_url = request.form.get("RecordingUrl") + ".wav"

    # --- Download recording ---
    audio_file = "caller_audio.wav"
    data = requests.get(recording_url).content
    with open(audio_file, "wb") as f:
        f.write(data)

    # --- Speech to Text ---
    recognizer = sr.Recognizer()
    try:
        with sr.AudioFile(audio_file) as source:
            audio = recognizer.record(source)
        text = recognizer.recognize_google(audio)
    except:
        text = "(voice not clear)"

    urgent = detect_urgent(text)

    response = VoiceResponse()

    # If message urgent → forward immediately to user number
    if urgent and state["user_number"]:
        response.say("Connecting you to the user right now, please wait.")

        # Dial user
        dial = Dial(caller_id=TWILIO_FROM)
        dial.number(state["user_number"])
        response.append(dial)

        return Response(str(response), mimetype="text/xml")

    # NOT URGENT → normal reply
    reply = llm_reply(text, state["mode"], state["custom_reason"])
    response.say(reply)
    response.hangup()

    return Response(str(response), mimetype="text/xml")


# -----------------------------
# 3) LLM RESPONSE
# -----------------------------
def llm_reply(user_text, mode, custom_reason):
    client = Groq(api_key=LLM_API_KEY)

    mode_text = {
        "sleep": "The user is sleeping.",
        "meeting": "The user is in a meeting.",
        "driving": "The user is driving.",
        "custom": custom_reason
    }.get(mode, "The user is unavailable.")

    prompt = f"""
You are a polite AI call agent. 
Caller said: "{user_text}"
User mode: {mode_text}
Give one short human-like sentence.
Do NOT mention AI or automation.
"""
    res = client.chat.completions.create(
        model="llama3-8b-8192",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=50
    )

    return res.choices[0].message["content"]


# -----------------------------
# Run Server
# -----------------------------
if __name__ == "__main__":
    app.run(port=5000, debug=True)
