"""Pytest configuration and fixtures for SproutForge tests."""

import os
import sys
from unittest.mock import MagicMock

# In the remio aApp embedded environment, remio_sdk is provided by the runtime.
# When running standalone unit tests outside remio, mock remio_sdk if absent.
if 'remio_sdk' not in sys.modules:
    mock_remio_sdk = MagicMock()
    mock_logger = MagicMock()
    mock_remio_sdk.create_aapp_logger.return_value = mock_logger
    sys.modules['remio_sdk'] = mock_remio_sdk
