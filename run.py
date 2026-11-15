"""Start the dashboard:  python run.py   then open http://localhost:8010"""
import json
import webbrowser
from pathlib import Path

import uvicorn

if __name__ == "__main__":
    port = json.loads((Path(__file__).parent / "config.json").read_text()).get("port", 8010)
    print(f"City dashboard: http://localhost:{port}   (Ctrl+C to stop)")
    try:
        webbrowser.open(f"http://localhost:{port}")
    except Exception:
        pass
    uvicorn.run("backend.app:app", host="127.0.0.1", port=port, log_level="warning")
