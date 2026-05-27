import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import _clean_env_key


class ConfigEnvTests(unittest.TestCase):
    def test_clean_env_key_strips_utf8_bom(self):
        self.assertEqual(_clean_env_key("\ufeffNVIDIA_API_KEY"), "NVIDIA_API_KEY")


if __name__ == "__main__":
    unittest.main()
