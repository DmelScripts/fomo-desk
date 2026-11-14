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

    def test_provider_keys_and_helius_query_are_scrubbed(self):
        os.environ["PYTH_API_KEY"] = "pyth-live-value"
        os.environ["HELIUS_API_KEY"] = "helius-live-value"
        os.environ["SOLANA_TRACKER_API_KEY"] = "tracker-live-value"
        self.addCleanup(lambda: os.environ.pop("PYTH_API_KEY", None))
        self.addCleanup(lambda: os.environ.pop("HELIUS_API_KEY", None))
        self.addCleanup(lambda: os.environ.pop("SOLANA_TRACKER_API_KEY", None))
        install_secret_redaction()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("desk.redact.providers")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        self.addCleanup(lambda: logger.removeHandler(handler))
        logger.info(
            "values %s %s %s",
            os.environ["PYTH_API_KEY"],
            os.environ["HELIUS_API_KEY"],
            os.environ["SOLANA_TRACKER_API_KEY"],
        )
        try:
            raise RuntimeError("https://mainnet.helius-rpc.com/?api-key=helius-live-value&x=1")
        except RuntimeError:
            logger.exception("helius failed")
        try:
            raise RuntimeError("https://mainnet.helius-rpc.com/?api-key=other-helius-token")
        except RuntimeError:
            logger.exception("other helius failed")
        text = stream.getvalue()
        self.assertNotIn("pyth-live-value", text)
        self.assertNotIn("helius-live-value", text)
        self.assertNotIn("tracker-live-value", text)
        self.assertNotIn("other-helius-token", text)
        self.assertIn("api-key=[REDACTED]", text)

    def test_access_log_keeps_its_fields_and_app_logs_still_emit(self):
        from uvicorn.logging import AccessFormatter
        secret = "helius-access-log-secret"
        os.environ["HELIUS_API_KEY"] = secret
        self.addCleanup(lambda: os.environ.pop("HELIUS_API_KEY", None))
        install_secret_redaction()
        stream = io.StringIO()
        access_handler = logging.StreamHandler(stream)
        access_handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s'))
        access = logging.getLogger("uvicorn.access")
        access.addHandler(access_handler)
        access.setLevel(logging.INFO)
        previous_propagate = access.propagate
        access.propagate = False
        app_handler = logging.StreamHandler(stream)
        app = logging.getLogger("desk.access.companion")
        app.addHandler(app_handler)
        app.setLevel(logging.INFO)
        self.addCleanup(lambda: access.removeHandler(access_handler))
        self.addCleanup(lambda: app.removeHandler(app_handler))
        self.addCleanup(lambda: setattr(access, "propagate", previous_propagate))
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1",
            "GET",
            "/health?api-key=" + secret,
            "1.1",
            200,
        )
        app.info("desk pass visible")
        text = stream.getvalue()
        self.assertIn("GET", text)
        self.assertIn("/health?api-key=[REDACTED]", text)
        self.assertIn("200", text)
        self.assertIn("desk pass visible", text)
        self.assertNotIn(secret, text)
        self.assertNotIn("ValueError", text)
