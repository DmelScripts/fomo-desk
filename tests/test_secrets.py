"""Secret values are redacted if a log line ever contains them."""
import io
import logging
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import install_secret_redaction


class TestSecrets(unittest.TestCase):
    def test_log_filter_redacts_secret_env_values(self):
        os.environ["TYPESAFE_API_KEY"] = "typesafe-test-secret"
        os.environ["APP_TOKEN"] = "app-token-test-secret"
        os.environ["VAPID_PRIVATE_KEY"] = "vapid-test-secret"
        self.addCleanup(lambda: os.environ.pop("TYPESAFE_API_KEY", None))
        self.addCleanup(lambda: os.environ.pop("APP_TOKEN", None))
        self.addCleanup(lambda: os.environ.pop("VAPID_PRIVATE_KEY", None))
        install_secret_redaction()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("desk.redact.test")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        self.addCleanup(lambda: logger.removeHandler(handler))
        logger.info(
            "values %s %s %s",
            os.environ["TYPESAFE_API_KEY"],
            os.environ["APP_TOKEN"],
            os.environ["VAPID_PRIVATE_KEY"],
        )
        text = stream.getvalue()
        self.assertNotIn("typesafe-test-secret", text)
        self.assertNotIn("app-token-test-secret", text)
        self.assertNotIn("vapid-test-secret", text)
        self.assertIn("[REDACTED]", text)
