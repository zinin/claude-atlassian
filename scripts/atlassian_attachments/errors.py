"""Коды выхода и исключения."""

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_NOT_FOUND = 2
EXIT_NO_CREDENTIALS = 3
EXIT_NETWORK = 4


class AttachmentError(Exception):
    """Ошибка, которую CLI превращает в код выхода."""

    exit_code = EXIT_PARTIAL


class NotFoundError(AttachmentError):
    exit_code = EXIT_NOT_FOUND


class CredentialsError(AttachmentError):
    exit_code = EXIT_NO_CREDENTIALS


class NetworkError(AttachmentError):
    exit_code = EXIT_NETWORK
