"""Dockerfile parsing, variable substitution and .dockerignore (lib/build.py)."""
import unittest

from helpers import ROOT  # noqa: F401
import build


class Parse(unittest.TestCase):
    def test_instructions_comments_continuations(self):
        text = """
# a comment
FROM alpine:3
RUN apk add \\
    # a comment inside a continuation
    curl \\
    git
env A=1
"""
        got = [(k, a.split()) for k, a, _ in build.parse(text)]   # (the shell ignores extra spaces)
        self.assertEqual(got, [("FROM", ["alpine:3"]), ("RUN", ["apk", "add", "curl", "git"]), ("ENV", ["A=1"])])

    def test_trailing_continuation(self):
        got = [(k, a) for k, a, _ in build.parse("RUN echo a \\\n")]
        self.assertEqual(got, [("RUN", "echo a")])


class Substitute(unittest.TestCase):
    scope = {"A": "x", "EMPTY": ""}

    def test_forms(self):
        s = build.substitute
        self.assertEqual(s("$A/${A}", self.scope), "x/x")
        self.assertEqual(s("${MISSING:-def}", self.scope), "def")
        self.assertEqual(s("${EMPTY:-def}", self.scope), "def")
        self.assertEqual(s("${A:-def}", self.scope), "x")
        self.assertEqual(s("${A:+alt}", self.scope), "alt")
        self.assertEqual(s("${MISSING:+alt}", self.scope), "")
        self.assertEqual(s("$MISSING", self.scope), "")


class KeyValues(unittest.TestCase):
    def test_env_forms(self):
        self.assertEqual(build.parse_kv('A=1 B="two words"', {}, "ENV"), {"A": "1", "B": "two words"})
        self.assertEqual(build.parse_kv("LEGACY some value", {}, "ENV"), {"LEGACY": "some value"})
        self.assertEqual(build.parse_kv("P=$HOME/bin", {"HOME": "/root"}, "ENV"), {"P": "/root/bin"})

    def test_arg_without_default(self):
        self.assertEqual(build.parse_kv("VERSION", {}, "ARG"), {"VERSION": None})


class ExecForm(unittest.TestCase):
    def test_json_and_shell(self):
        self.assertEqual(build.exec_form('["python", "-m", "app"]'), ["python", "-m", "app"])
        self.assertIsNone(build.exec_form("python -m app"))


class Ignore(unittest.TestCase):
    def test_patterns(self):
        pats = ["*.pyc", "build", "!build/keep.txt", "**/node_modules"]
        self.assertTrue(build.ignored("x.pyc", pats))
        self.assertTrue(build.ignored("build/out.o", pats))
        self.assertFalse(build.ignored("build/keep.txt", pats))
        self.assertTrue(build.ignored("web/node_modules", pats))
        self.assertFalse(build.ignored("src/main.py", pats))


if __name__ == "__main__":
    unittest.main()
