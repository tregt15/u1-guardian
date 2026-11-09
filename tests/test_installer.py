"""Ollama installer flow with a harmless fake command (python prints text). Nothing is installed."""

import sys

from guardian import hostinfo
from guardian.ai.installer import OllamaInstaller


def fake_plan(code=0, text="Successfully installed"):
    script = (f"import sys; sys.stdout.buffer.write({text!r}.encode() + b'\\n' + '  \u2588\u2588\u2592\u2592  '.encode('utf-8') "
              f"+ b'\\r\\n'); sys.stdout.flush(); sys.exit({code})")
    cmd = [sys.executable, "-c", script]
    return lambda: {"can_install": True, "method": "fake", "command": cmd, "manual": "do it by hand"}


class Version:
    """Ollama answers after `after` calls."""

    def __init__(self, after):
        self.after, self.calls = after, 0

    async def __call__(self):
        self.calls += 1
        return "0.40.15" if self.calls > self.after else None


async def test_install_then_ollama_answers():
    inst = OllamaInstaller(Version(after=2), plan=fake_plan(), start_cmd=lambda: None, start=lambda c: None,
                           wait_s=1, poll_s=0.01)
    await inst.install()
    st = inst.state.as_dict()
    assert st["done"] and st["error"] is None and not st["running"]
    assert "Successfully installed" in st["output"] and not any("██" in x for x in st["output"])


async def test_already_running_skips_the_installer():
    started = []
    inst = OllamaInstaller(Version(after=0), plan=fake_plan(code=1), start_cmd=lambda: None,
                           start=started.append, wait_s=0.05, poll_s=0.01)
    await inst.install()
    assert inst.state.done and inst.state.output == type(inst.state.output)(maxlen=15)


async def test_installer_error_is_reported():
    inst = OllamaInstaller(Version(after=99), plan=fake_plan(code=3, text="boom"), start_cmd=lambda: None,
                           start=lambda c: None, wait_s=0.05, poll_s=0.01)
    await inst.install()
    assert not inst.state.done and "code 3" in inst.state.error


async def test_already_installed_text_counts_as_success_and_starts_ollama():
    started = []
    version = Version(after=10**6)

    def start(cmd):
        started.append(cmd)
        version.after = 0  # Ollama answers once started

    inst = OllamaInstaller(version, plan=fake_plan(code=5, text="Found an existing package already installed."),
                           start_cmd=lambda: ["ollama app.exe"], start=start, wait_s=0.05, poll_s=0.01)
    await inst.install()
    assert started == [["ollama app.exe"]] and inst.state.done


async def test_cannot_install_here_gives_the_manual_steps():
    inst = OllamaInstaller(Version(after=99), plan=lambda: {"can_install": False, "command": None,
                                                            "manual": "Run this in a terminal"})
    await inst.install()
    assert inst.state.error == "Run this in a terminal"
    assert "command" not in inst.plan()


def test_real_plan_shape():
    plan = hostinfo.ollama_install_plan()
    assert set(plan) >= {"can_install", "method", "command", "manual"}
    if plan["can_install"]:
        assert plan["command"][0] == "winget" and "Ollama.Ollama" in plan["command"]
