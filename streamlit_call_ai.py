# streamlit_app.py
import streamlit as st
import requests
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()

# Config
FLASK_BASE = os.getenv("FLASK_BASE", "http://localhost:5000")  # where Flask app runs
TW_SID = os.getenv("TWILIO_ACCOUNT_SID")
TW_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TW_FROM = os.getenv("TWILIO_NUMBER")       # Twilio 'from' (your Twilio number)
AI_NUMBER = os.getenv("TWILIO_AI_NUMBER")  # Twilio AI number that receives forwarded calls
BASE_URL = os.getenv("BASE_URL")           # ngrok public URL (for Twilio to fetch /play-audio)
AUDIO_URGENT = "/mnt/data/test_caller_urgent.wav"
AUDIO_NOTURG = "/mnt/data/test_caller_noturgent.wav"

st.set_page_config(page_title="Call.AI (Mobile UI)", page_icon="📞", layout="centered")

st.title("Call.AI — Mobile UI (Streamlit)")

menu = st.sidebar.selectbox("Menu", ["Welcome", "Enter Number", "Modes", "Forwarding Setup", "Test Call", "Docs"])

if menu == "Welcome":
    st.header("Welcome to Call.AI")
    st.write("Mobile-style app used to set modes and manage call forwarding to the AI assistant.")
    st.info("This Streamlit app is a development/mobile UI. The actual inbound handling runs in Flask + Twilio.")

if menu == "Enter Number":
    st.header("Enter your mobile number (E.164)")
    user_number = st.text_input("Number (e.g. +9198XXXXXXXX)", value=st.session_state.get("user_number",""))
    if st.button("Save Number"):
        # save to Flask via /set-mode with duration 0? better provide separate endpoint but use file approach via set-mode later
        st.session_state["user_number"] = user_number
        # Make a call to Flask status to update user_number without activating mode
        resp = requests.post(f"{FLASK_BASE}/set-mode", json={
            "mode": "normal", "reason": "", "duration": 1, "user_number": user_number
        })
        # Immediately clear mode so it's not active
        requests.post(f"{FLASK_BASE}/clear-mode")
        st.success("Saved number and updated backend.")

    if st.session_state.get("user_number"):
        st.info(f"Saved: {st.session_state.get('user_number')}")

if menu == "Modes":
    st.header("Set Mode (sleep / meeting / driving / custom)")
    cols = st.columns(2)
    with cols[0]:
        mode = st.selectbox("Mode", ["sleep", "meeting", "driving", "custom"])
    with cols[1]:
        minutes = st.slider("Duration (minutes)", 1, 60, 10)

    reason = ""
    if mode == "custom":
        reason = st.text_input("Reason (short)")

    if st.button("Activate Mode"):
        user_number = st.session_state.get("user_number")
        if not user_number:
            st.warning("Enter your number first on the 'Enter Number' page.")
        else:
            payload = {"mode": mode, "reason": reason, "duration": minutes, "user_number": user_number}
            r = requests.post(f"{FLASK_BASE}/set-mode", json=payload)
            if r.ok:
                st.success("Mode activated.")
            else:
                st.error("Failed to activate mode. Is Flask running?")

    if st.button("Clear Mode"):
        r = requests.post(f"{FLASK_BASE}/clear-mode")
        if r.ok:
            st.success("Mode cleared.")
        else:
            st.error("Failed to clear mode.")

    # show status
    r = requests.get(f"{FLASK_BASE}/status")
    if r.ok:
        s = r.json()
        st.write("Backend mode status:")
        st.json(s)

if menu == "Forwarding Setup":
    st.header("Auto-open dialer on mobile with forwarding code")
    st.write("This shows the forwarding code you can tap on mobile to open the dialer with the code prefilled.")
    if AI_NUMBER:
        forward_code = f"**61*{AI_NUMBER}#"
        tel_link = f"tel:{forward_code}"
        st.markdown(f"[Open dialer and apply 'No-answer' forward → {forward_code}]({tel_link})")
        st.write("On mobile this will open the dialer with the code. On desktop it will likely do nothing.")
    else:
        st.error("TWILIO_AI_NUMBER not set in .env")

if menu == "Test Call":
    st.header("Trigger a test inbound flow (Twilio outbound -> AI receives audio)")
    st.write("Use this to simulate an incoming caller (no phone required).")
    sim = st.selectbox("Simulate", ["urgent audio", "not-urgent audio", "text sample"])
    if sim != "text sample":
        filename = "test_caller_urgent.wav" if sim == "urgent audio" else "test_caller_noturgent.wav"
        if st.button("Start test call (play audio to AI)"):
            if not all([TW_SID, TW_TOKEN, TW_FROM, AI_NUMBER, BASE_URL]):
                st.error("Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_NUMBER, TWILIO_AI_NUMBER and BASE_URL in .env")
            else:
                client = Client(TW_SID, TW_TOKEN)
                play_url = f"{BASE_URL}/play-audio?file={filename}"
                call = client.calls.create(to=AI_NUMBER, from_=TW_FROM, url=play_url)
                st.success(f"Test call started, SID: {call.sid}")
                st.info("Watch Flask logs for incoming-call -> process-recording behavior.")
    else:
        text = st.text_area("Caller text", "This is urgent. Please connect me.")
        if st.button("Run text simulation (local)"):
            # local simulation - call Flask endpoints directly (no Twilio)
            # emulate incoming-call -> process_recording by calling /process-recording not trivial; instead call /status and then check is_urgent logic via a small endpoint
            r = requests.post(f"{FLASK_BASE}/set-mode", json={"mode":"sleep","reason":"testing","duration":5,"user_number":st.session_state.get("user_number")})
            st.info("Mode set for 5 minutes for test.")
            # Call backend urgent detection endpoint (we don't have standalone detection endpoint), we will call the process flow by simulating STT result:
            # For simplicity, do a small POST to /simulate-text (not present); instead call Groq directly here or use keyword fallback
            keywords = ["urgent","emergency","important","immediately","help","asap"]
            is_urgent = any(k in text.lower() for k in keywords)
            if is_urgent:
                st.success("Detected URGENT (keyword fallback). Would forward to user number if set.")
            else:
                st.info("Detected NOT urgent.")
            st.write("Simulated AI reply:")
            if is_urgent:
                st.write("Connecting you to the user now...")
            else:
                st.write("User is currently unavailable. I will notify them.")

if menu == "Docs":
    st.header("Docs & File")
    st.write("Local business PDF (uploaded file):")
    # developer requested to include the uploaded file path
    pdf_path = "/mnt/data/ai_agency_business_revenue_models.pdf"
    if os.path.exists(pdf_path):
        st.download_button("Download business PDF", pdf_path)
    else:
        st.info("PDF not found at path: " + pdf_path)

st.write("")  # small footer
