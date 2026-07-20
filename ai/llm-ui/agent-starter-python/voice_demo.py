import os
import json
import subprocess
import tempfile
from pathlib import Path
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv

load_dotenv(".env.local")
CARTESIA_KEY = os.environ.get("CARTESIA_API_KEY")

app = Flask(__name__)
BASE = Path(__file__).resolve().parent
VOICE_FILE = BASE / "current_voice.txt"
LIBRARY_FILE = BASE / "voices.json"


def load_library():
    try:
        return json.loads(LIBRARY_FILE.read_text())
    except Exception:
        return {}


def save_library(lib):
    LIBRARY_FILE.write_text(json.dumps(lib, indent=2))


def deploy_voice(voice_id):
    VOICE_FILE.write_text(voice_id + "\n")
    subprocess.run(["sudo", "-n", "systemctl", "restart", "llm-agent"], check=True)


PAGE = """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bumblebee Voice Demo</title>
  <style>
    body { font-family: -apple-system, sans-serif; text-align: center;
           padding: 30px 20px; background: #1a1a2e; color: #eee; }
    h1 { font-size: 1.5em; }
    h2 { font-size: 1.1em; color: #aaa; margin-top: 36px; }
    .consent { font-size: 0.8em; color: #888; margin: 12px 10px; }
    input[type=text] { font-size: 1.1em; padding: 12px; border-radius: 10px;
                       border: none; width: 80%; max-width: 300px; margin: 8px 0; }
    input[type=file] { font-size: 1em; margin: 12px 0; color: #eee; width: 90%; }
    button { font-size: 1.1em; padding: 14px 24px; margin: 8px;
             border: none; border-radius: 12px; background: #e94560;
             color: white; }
    button:disabled { background: #555; }
    .use-btn { background: #0f3460; }
    #status { margin: 20px 10px; font-size: 1.05em; min-height: 30px; }
    .voice-row { margin: 8px 0; }
  </style>
</head>
<body>
  <h1>Bumblebee Voice Demo</h1>
  <p class="consent">By uploading, you agree to have your voice
     cloned for this demo.</p>
  <input type="text" id="voiceName" placeholder="Name this voice (e.g. Teresa)">
  <br>
  <input type="file" id="fileInput">
  <br>
  <button id="deployBtn" onclick="upload()">Clone &amp; Deploy</button>
  <div id="status">Name a voice and choose a memo to begin</div>
  <h2>Saved voices</h2>
  <div id="library">Loading...</div>
  <script>
    async function refreshLibrary() {
      const r = await fetch('/voices');
      const lib = await r.json();
      const div = document.getElementById('library');
      const names = Object.keys(lib);
      if (!names.length) { div.textContent = 'No saved voices yet.'; return; }
      div.innerHTML = '';
      names.forEach(name => {
        const row = document.createElement('div');
        row.className = 'voice-row';
        const btn = document.createElement('button');
        btn.className = 'use-btn';
        btn.textContent = 'Use ' + name;
        btn.onclick = () => useVoice(name);
        row.appendChild(btn);
        div.appendChild(row);
      });
    }
    async function useVoice(name) {
      const status = document.getElementById('status');
      status.textContent = 'Switching to ' + name + '... (~5 sec)';
      const r = await fetch('/use', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: name})
      });
      const data = await r.json();
      status.textContent = data.message;
    }
    async function upload() {
      const status = document.getElementById('status');
      const fileInput = document.getElementById('fileInput');
      const nameBox = document.getElementById('voiceName');
      const btn = document.getElementById('deployBtn');
      if (!nameBox.value.trim()) { status.textContent = 'Please name the voice first.'; return; }
      if (!fileInput.files.length) { status.textContent = 'Please choose a file first.'; return; }
      btn.disabled = true;
      status.textContent = 'Cloning ' + nameBox.value + '... (a few seconds)';
      const form = new FormData();
      form.append('clip', fileInput.files[0]);
      form.append('name', nameBox.value.trim());
      try {
        const r = await fetch('/clone_deploy', {method: 'POST', body: form});
        const data = await r.json();
        status.textContent = data.message;
        refreshLibrary();
      } catch (err) {
        status.textContent = 'Upload error: ' + err.message;
      }
      btn.disabled = false;
    }
    refreshLibrary();
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return PAGE


@app.route("/voices")
def voices():
    return jsonify(load_library())


@app.route("/use", methods=["POST"])
def use():
    name = request.json.get("name")
    lib = load_library()
    if name not in lib:
        return jsonify({"message": "No saved voice named " + str(name)})
    try:
        deploy_voice(lib[name])
        return jsonify({"message": "Now talking as " + name + "! Talk to Bumblebee in ~5 sec."})
    except Exception as e:
        return jsonify({"message": "Error: " + str(e)})


@app.route("/clone_deploy", methods=["POST"])
def clone_deploy():
    if "clip" not in request.files:
        return jsonify({"message": "No audio received."})
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify({"message": "No name provided."})
    clip = request.files["clip"]
    orig_name = clip.filename or "voice.wav"
    try:
        with tempfile.NamedTemporaryFile(suffix="_" + orig_name, delete=False) as tmp:
            clip.save(tmp.name)
            tmp_path = tmp.name
        with open(tmp_path, "rb") as f:
            resp = requests.post(
                "https://api.cartesia.ai/voices/clone",
                headers={
                    "Authorization": "Bearer " + CARTESIA_KEY,
                    "Cartesia-Version": "2025-04-16",
                },
                files={"clip": (orig_name, f, clip.mimetype or "audio/wav")},
                data={"name": name, "language": "en"},
            )
        os.unlink(tmp_path)
        if resp.status_code != 200:
            return jsonify({"message": "Cartesia error " + str(resp.status_code) + ": " + resp.text[:200]})
        voice_id = resp.json()["id"]
        lib = load_library()
        lib[name] = voice_id
        save_library(lib)
        deploy_voice(voice_id)
        return jsonify({"message": "Saved & deployed " + name + "! Talk to Bumblebee in ~5 sec."})
    except Exception as e:
        return jsonify({"message": "Error: " + str(e)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
