"""Start the optional portable Ollama without changing global user settings."""
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

runtime = Path(__file__).resolve().parents[1] / 'runtime'


def available():
    try:
        with urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=2) as response:
            print('Local models:', [m['name'] for m in json.load(response)['models']])
        return True
    except OSError:
        return False


def start():
    if available():
        return
    binary = runtime / 'ollama/ollama.exe'
    if not binary.exists():
        raise SystemExit('Portable Ollama missing. Install Ollama and pull qwen3-vl:4b-instruct; see README.md.')
    profile = runtime / 'ollama-profile'
    profile.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(USERPROFILE=str(profile), OLLAMA_HOST='127.0.0.1:11434',
               OLLAMA_MODELS=str(runtime / 'ollama-models'), OLLAMA_NO_CLOUD='1', OLLAMA_CONTEXT_LENGTH='4096')
    with (runtime / 'ollama-serve.out.log').open('w') as out, (runtime / 'ollama-serve.err.log').open('w') as err:
        process = subprocess.Popen([str(binary), 'serve'], env=env, stdout=out, stderr=err,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    (runtime / 'ollama-server.pid').write_text(str(process.pid))
    for _ in range(60):
        if available(): return
        if process.poll() is not None: break
        time.sleep(.5)
    raise SystemExit('Ollama did not start. Check runtime/ollama-serve.err.log.')


if __name__ == '__main__':
    start()
