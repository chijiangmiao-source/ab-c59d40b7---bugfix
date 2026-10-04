"""Entry point: python -m app.main [--host 0.0.0.0] [--port 8080]."""

import argparse
import os

from .server import make_server


def main(argv=None):
    ap = argparse.ArgumentParser(description="Diagnostic class verifier")
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT", "8080")))
    args = ap.parse_args(argv)
    httpd = make_server(args.host, args.port)
    print("verifier listening on %s:%d" % (args.host, args.port),
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
