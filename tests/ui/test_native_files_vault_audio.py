from __future__ import annotations

from pathlib import Path
import wave

from PyQt6.QtCore import Qt
from PyQt6.QtMultimedia import QMediaPlayer

from src.app.ui.ai_library_window import AILibraryWindow
from src.app.ui.audio_preview_window import AudioPreviewWindow
from src.shared.vault.client import VaultStats


class _Vault:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.online = True

    def is_available(self) -> bool:
        return self.online

    def search(self, text: str, *, limit: int) -> list[dict[str, str]]:
        assert text == "image" and limit == 100
        return [{"name": self.path.name, "path": str(self.path)}]

    def get_stats(self) -> VaultStats:
        return VaultStats(total=1)


def test_vault_result_activation_and_offline_state(qtbot, tmp_path):
    image = tmp_path / "image.png"
    client = _Vault(image)
    window = AILibraryWindow(client=client)
    qtbot.addWidget(window)
    window.search.setText("image")
    window.query()
    qtbot.waitUntil(lambda: window.results.count() == 1)
    with qtbot.waitSignal(window.path_requested) as activated:
        window.results.itemActivated.emit(window.results.item(0))
    assert activated.args == [str(image)]
    assert window.results.item(0).data(Qt.ItemDataRole.UserRole) == str(image)
    client.online = False
    window.query()
    qtbot.waitUntil(lambda: "离线" in window.notice.text())
    assert window.results.count() == 0


def test_audio_open_is_idle_and_close_releases_the_source(qtbot, tmp_path):
    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * 8000)
    window = AudioPreviewWindow()
    qtbot.addWidget(window)
    window.set_paths((str(path),))
    assert window._player.source().toLocalFile() == str(path)
    assert window._player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
    window.show()
    with qtbot.waitSignal(window.closed):
        window.close()
    assert window._player.source().isEmpty()
