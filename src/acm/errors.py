class AcmError(Exception):
    """Error with a stable machine-readable code, safe to show to the user."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
