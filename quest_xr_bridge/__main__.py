"""Standalone launcher using the same embedded SDK lifetime."""

import logging
import os
import signal
import threading

from quest_xr_bridge.sdk import QuestServer

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    service = QuestServer(
        host=os.environ.get("POSE_HOST", "0.0.0.0"),
        port=int(os.environ.get("POSE_PORT", "8000")),
        record_poses=os.environ.get("POSE_LOG_ENABLED", "0").lower() in {"1", "true", "yes", "on"},
        cert_file=os.environ.get("POSE_CERT_FILE"),
        key_file=os.environ.get("POSE_KEY_FILE"),
    )
    try:
        with service:
            logger.info("Quest page: %s/", service.url)
            logger.info("PC viewer: %s/viewer", service.url)
            stopped.wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
