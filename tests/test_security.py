import base64
import json
import os
import tempfile
import unittest
from unittest import mock

from werkzeug.exceptions import NotFound

import app as app_module


def _basic_auth(username, password):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class ViewerSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.previous_output = app_module.app.config["OUTPUT_FOLDER"]
        app_module.app.config.update(
            TESTING=True,
            OUTPUT_FOLDER=self.tempdir.name,
        )
        self.client = app_module.app.test_client()

    def tearDown(self):
        app_module.app.config["OUTPUT_FOLDER"] = self.previous_output
        self.tempdir.cleanup()

    def test_auth_is_disabled_by_default(self):
        with mock.patch.object(app_module, "VIEWER_AUTH_USER", ""), mock.patch.object(
            app_module, "VIEWER_AUTH_PASSWORD", ""
        ):
            response = self.client.get("/api/docs")
        self.assertEqual(response.status_code, 200)

    def test_auth_rejects_missing_and_wrong_credentials(self):
        with mock.patch.object(
            app_module, "VIEWER_AUTH_USER", "reviewer"
        ), mock.patch.object(app_module, "VIEWER_AUTH_PASSWORD", "secret"):
            missing = self.client.get("/api/docs")
            wrong = self.client.get(
                "/api/docs", headers=_basic_auth("reviewer", "wrong")
            )
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertIn("Basic", missing.headers["WWW-Authenticate"])

    def test_auth_accepts_valid_credentials(self):
        with mock.patch.object(
            app_module, "VIEWER_AUTH_USER", "reviewer"
        ), mock.patch.object(app_module, "VIEWER_AUTH_PASSWORD", "secret"):
            response = self.client.get(
                "/api/docs", headers=_basic_auth("reviewer", "secret")
            )
        self.assertEqual(response.status_code, 200)

    def test_document_responses_are_not_cacheable(self):
        response = self.client.get("/api/docs")
        self.assertEqual(response.headers["Cache-Control"], "private, no-store, max-age=0")
        self.assertEqual(response.headers["Pragma"], "no-cache")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertIn("connect-src 'self'", response.headers["Content-Security-Policy"])

    def test_remote_bind_requires_auth_or_explicit_override(self):
        with mock.patch.object(app_module, "VIEWER_AUTH_USER", ""), mock.patch.object(
            app_module, "ALLOW_UNAUTHENTICATED_REMOTE", False
        ):
            app_module.validate_viewer_exposure("127.0.0.1")
            with self.assertRaises(RuntimeError):
                app_module.validate_viewer_exposure("0.0.0.0")

        with mock.patch.object(
            app_module, "VIEWER_AUTH_USER", "reviewer"
        ), mock.patch.object(app_module, "ALLOW_UNAUTHENTICATED_REMOTE", False):
            app_module.validate_viewer_exposure("0.0.0.0")

        with mock.patch.object(app_module, "VIEWER_AUTH_USER", ""), mock.patch.object(
            app_module, "ALLOW_UNAUTHENTICATED_REMOTE", True
        ):
            app_module.validate_viewer_exposure("0.0.0.0")

    def test_document_directory_rejects_normalized_path_aliases(self):
        os.mkdir(os.path.join(self.tempdir.name, "sample"))
        with app_module.app.test_request_context():
            with self.assertRaises(NotFound):
                app_module._doc_dir("../sample")

    def test_optional_review_report_marks_documents_and_pages(self):
        doc_dir = os.path.join(self.tempdir.name, "sample")
        os.mkdir(doc_dir)
        with open(
            os.path.join(doc_dir, "page_0001_structured.json"),
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump({"page_number": 1, "elements": []}, handle)

        report_path = os.path.join(self.tempdir.name, "review.json")
        with open(report_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "failed_pages": [
                        {
                            "doc": "sample",
                            "page": 1,
                            "score": 60,
                            "severity": "major",
                            "issue_types": ["table_structure"],
                            "reason": "primary finding",
                            "reviewed": True,
                            "review_verdict": {
                                "confirmed_failure": False,
                                "reason": "follow-up finding",
                            },
                        }
                    ]
                },
                handle,
            )

        with mock.patch.object(app_module, "VIEWER_REVIEW_REPORT", report_path):
            docs = self.client.get("/api/docs").get_json()
            detail = self.client.get("/api/doc/sample").get_json()

        self.assertEqual(docs[0]["review_pages"], [1])
        review = detail["pages"][0]["review"]
        self.assertEqual(review["issue_types"], ["table_structure"])
        self.assertIs(review["review_confirmed"], False)
        self.assertEqual(review["review_reason"], "follow-up finding")


if __name__ == "__main__":
    unittest.main()
