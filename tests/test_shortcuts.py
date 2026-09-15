"""Starting like an app: the icon file, the shortcut, a console-less start.

Nothing here touches the real Desktop or Start menu - shortcuts are written into
the test's own temporary folder and read back from there.
"""

import io
import os
import struct
import subprocess
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from lecture_saver.app import ensure_output_streams  # noqa: E402
from lecture_saver.shortcuts import ICON_SIZES, create_shortcut, icon_bytes  # noqa: E402

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def icon_file(qapp, tmp_path):
    path = tmp_path / "lecture_saver.ico"
    path.write_bytes(icon_bytes())
    return path


class TestIcon:
    def test_it_is_an_icon_file_with_every_size(self, qapp):
        data = icon_bytes()
        reserved, kind, count = struct.unpack_from("<HHH", data, 0)

        assert (reserved, kind) == (0, 1)
        assert count == len(ICON_SIZES)

        sizes = []
        for index in range(count):
            width, height, _, _, planes, bits, length, offset = struct.unpack_from(
                "<BBBBHHII", data, 6 + 16 * index
            )
            sizes.append(width or 256)
            assert width == height
            assert (planes, bits) == (1, 32)
            assert data[offset : offset + 8] == PNG_SIGNATURE
            assert offset + length <= len(data)
        assert sizes == list(ICON_SIZES)


class TestConsoleLessStart:
    def test_missing_output_streams_are_replaced(self, monkeypatch):
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)

        ensure_output_streams()

        # A library writing progress to stderr must not crash the app.
        sys.stderr.write("progress 50%\r")
        sys.stdout.write("hello\n")

    def test_real_streams_are_left_alone(self, monkeypatch):
        out, err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)

        ensure_output_streams()

        assert sys.stdout is out and sys.stderr is err


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcuts")
class TestShortcut:
    def test_it_starts_this_checkout_without_a_console(self, icon_file, tmp_path):
        link = create_shortcut(tmp_path / "Lecture Saver.lnk", icon=icon_file)
        assert link.is_file()

        read_back = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LS_LINK);"
                "$s.TargetPath; $s.Arguments; $s.WorkingDirectory; $s.IconLocation",
            ],
            env=dict(os.environ, LS_LINK=str(link)),
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        target, arguments, workdir, icon = (line.strip() for line in read_back[:4])

        assert target.lower().endswith("pythonw.exe")
        assert arguments.startswith('"') and arguments.rstrip('"').endswith("run.py")
        assert (os.path.join(workdir, "run.py")).lower() == arguments.strip('"').lower()
        assert icon.lower().startswith(str(icon_file).lower())

    def test_a_folder_with_spaces_and_korean_is_fine(self, icon_file, tmp_path):
        link = create_shortcut(tmp_path / "강의 노트" / "Lecture Saver.lnk", icon=icon_file)
        assert link.is_file()
