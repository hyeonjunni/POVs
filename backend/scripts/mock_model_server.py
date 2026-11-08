import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class MockHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length) if content_length else b"{}"
        payload = json.loads(raw.decode("utf-8"))

        if self.path == "/generate":
            model_id = payload.get("model_id", "unknown-model")
            input_text = payload.get("input", "")
            response = {
                "output_text": f"[Gemma mock] {model_id} received {len(input_text)} chars of prompt.",
            }
        elif self.path == "/chat":
            model_id = payload.get("model", "unknown-model")
            messages = payload.get("messages", [])
            response = {
                "choices": [
                    {
                        "message": {
                            "content": f"[EXAONE mock] {model_id} received {len(messages)} message entries."
                        }
                    }
                ]
            }
        else:
            self.send_response(404)
            self.end_headers()
            return

        body = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 9001), MockHandler)
    print("Mock model server listening on http://127.0.0.1:9001")
    server.serve_forever()
