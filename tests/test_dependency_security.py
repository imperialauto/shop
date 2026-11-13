"""Guard against reintroducing known-vulnerable parser/template dependency pins.

This is package metadata verification, not a substitute for an SCA scan.
Run in the existing GitHub Actions disposable environment.
"""
import unittest
from importlib.metadata import version


def major_minor_patch(distribution):
    """Both patched lower bounds are normal three-component PyPI releases."""
    return tuple(int(x) for x in version(distribution).split(".")[:3])


class SecurityDependencyBaseline(unittest.TestCase):
    def test_multipart_header_limit_fix_is_installed(self):
        self.assertGreaterEqual(
            major_minor_patch("python-multipart"), (0, 0, 27),
            "CVE-2026-42561: multipart header parsing must use a fixed release",
        )

    def test_jinja_sandbox_fix_is_installed(self):
        self.assertGreaterEqual(
            major_minor_patch("jinja2"), (3, 1, 6),
            "CVE-2025-27516: Jinja2 must use a fixed release",
        )

    def test_starlette_and_cryptography_fixes_are_installed(self):
        self.assertGreaterEqual(major_minor_patch("starlette"), (1, 3, 1),
                                "Starlette releases before 1.3.1 have published request-handling vulnerabilities")
        self.assertGreaterEqual(major_minor_patch("cryptography"), (49, 0, 0),
                                "cryptography releases before 49 have published vulnerabilities")


if __name__ == "__main__":
    unittest.main()
