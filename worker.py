"""
Railway entry point for Gustavo (news bot).

Unlike the GitHub Actions workflow (which runs main.py once per cron tick and
commits the JSON files back to the repo), Railway runs this as a long-lived
process:

  - A background thread calls Gustavo().run() on a loop, every
    RUN_INTERVAL_MINUTES (default 5), so new news gets fetched and posted to
    Telegram immediately without waiting on GitHub Actions' cron delay.
  - The main thread runs a tiny HTTP server on $PORT that serves the current
    directory (index.html + news.json + market.json + ...), so the dashboard
    is reachable at your Railway public URL and Railway's health check has
    something to hit.

Data (news.json, market.json, etc.) is written to disk in the working
directory. Attach a Railway Volume mounted at this directory if you want that
history to survive redeploys/restarts - otherwise it resets on each deploy.
"""
import os
import time
import logging
import threading
import http.server
import socketserver

from main import Gustavo, logger

RUN_INTERVAL_MINUTES = int(os.environ.get("RUN_INTERVAL_MINUTES", "30"))
PORT = int(os.environ.get("PORT", "8080"))


def run_loop():
    while True:
        try:
            logger.info(">>> Starting scrape/post cycle")
            Gustavo().run()
        except Exception as e:
            logger.error(f"Unhandled error in run cycle: {e}")
        logger.info(f">>> Sleeping {RUN_INTERVAL_MINUTES} minute(s) before next cycle")
        time.sleep(RUN_INTERVAL_MINUTES * 60)


def serve_dashboard():
    handler = http.server.SimpleHTTPRequestHandler
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("0.0.0.0", PORT), handler) as httpd:
        logger.info(f">>> Dashboard server listening on port {PORT}")
        httpd.serve_forever()


if __name__ == "__main__":
    worker_thread = threading.Thread(target=run_loop, daemon=True)
    worker_thread.start()
    serve_dashboard()