import unittest

from atlassian_attachments.errors import (
    EXIT_NETWORK,
    EXIT_NO_CREDENTIALS,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_PARTIAL,
    AttachmentError,
    CredentialsError,
    NetworkError,
    NotFoundError,
)


class ExitCodesTest(unittest.TestCase):
    def test_codes_match_the_spec(self):
        self.assertEqual((EXIT_OK, EXIT_PARTIAL, EXIT_NOT_FOUND), (0, 1, 2))
        self.assertEqual((EXIT_NO_CREDENTIALS, EXIT_NETWORK), (3, 4))

    def test_each_error_carries_its_exit_code(self):
        self.assertEqual(NotFoundError("нет тикета").exit_code, EXIT_NOT_FOUND)
        self.assertEqual(CredentialsError("нет токена").exit_code, EXIT_NO_CREDENTIALS)
        self.assertEqual(NetworkError("таймаут").exit_code, EXIT_NETWORK)

    def test_all_errors_share_one_base(self):
        for err in (NotFoundError("a"), CredentialsError("b"), NetworkError("c")):
            self.assertIsInstance(err, AttachmentError)
