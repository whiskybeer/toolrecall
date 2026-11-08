import http.server, json, sys, itertools

OUT = sys.argv[1]
n = itertools.count()


class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        b = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        i = next(n)
        open(f"{OUT}/{i:03d}.json", "wb").write(b)
        open(f"{OUT}/{i:03d}.hdr", "w").write(
            self.path
            + "\n"
            + "".join(
                f"{k}: {v}\n"
                for k, v in self.headers.items()
                if k.lower() not in ("authorization", "x-api-key")
            )
        )
        self.send_response(400)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(
            b'{"type":"error","error":{"type":"invalid_request_error","message":"capture"}}'
        )

    def log_message(self, *a):
        pass


http.server.HTTPServer(("127.0.0.1", int(sys.argv[2])), H).serve_forever()
