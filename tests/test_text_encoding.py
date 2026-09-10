import tempfile
import unittest
from pathlib import Path

from tools.text_encoding import read_portable_text


class PortableTextTests(unittest.TestCase):
    def test_reads_utf8_bom_utf16_and_gb18030(self):
        samples = (
            ("utf-8-sig", "CC_USR_SUBMITTER=张三\n"),
            ("utf-16", "CC_USR_SUBMITTER=李四\n"),
            ("gb18030", "CC_USR_SUBMITTER=王五\n"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (encoding, expected) in enumerate(samples):
                path = root / f"env-{index}"
                path.write_bytes(expected.encode(encoding))
                self.assertEqual(read_portable_text(path), expected)


if __name__ == "__main__":
    unittest.main()
