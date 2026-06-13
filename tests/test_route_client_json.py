import json
import unittest

from fastapi import HTTPException

from mn_api.routes.client_json import client_json_response


class TestClientJsonResponse(unittest.TestCase):
    def test_decodes_client_json_payload(self):
        self.assertEqual(client_json_response(lambda: '{"ok": true}'), {"ok": True})

    def test_adapts_decode_errors_to_existing_error_response(self):
        response = client_json_response(lambda: "{not-json")

        self.assertEqual(response.status_code, 500)
        self.assertIn("Expecting property name enclosed in double quotes", json.loads(response.body)["error"])

    def test_can_preserve_http_exception_for_routes_that_already_raise_it(self):
        def raise_http_exception():
            raise HTTPException(status_code=422, detail="invalid")

        with self.assertRaises(HTTPException) as context:
            client_json_response(
                raise_http_exception,
                preserve_http_exceptions=True,
            )

        self.assertEqual(context.exception.status_code, 422)
