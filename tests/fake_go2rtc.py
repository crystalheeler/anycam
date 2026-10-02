
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
assert sys.argv[1] == "-config", sys.argv
conf = json.loads(sys.argv[2])
with open(os.environ["FAKE_RECORD"], "a") as f:
    f.write(sys.argv[2] + "\n")
host, port = conf["api"]["listen"].rsplit(":", 1)
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a):
        pass
srv = HTTPServer((host, int(port)), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
print("\x1b[33mWRN\x1b[0m [rtsp] dial rtsp://admin:hunter2@10.0.0.33:554/ch1 refused", flush=True)
time.sleep(float(os.environ.get("FAKE_LIFETIME", "1.5")))
srv.shutdown()
sys.exit(3)
