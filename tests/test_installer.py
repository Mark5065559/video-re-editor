import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "deploy" / "oracle" / "install.sh"
spec = importlib.util.spec_from_file_location("build_installer", ROOT / "deploy" / "oracle" / "build_installer.py")
build_installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_installer)

TOKEN = "111111111:" + "A" * 35
API_HASH = "0123456789abcdef0123456789abcdef"


def test_template_has_placeholders_and_no_real_values():
    text = TEMPLATE.read_text(encoding="utf-8")
    for placeholder in ("__BOT_TOKEN__", "__TG_API_ID__", "__TG_API_HASH__", "__REPO_URL__", "__BRANCHES__"):
        assert placeholder in text
    assert "\r\n" not in text  # must stay LF for Linux


def test_built_installer_is_valid_bash_and_small(tmp_path):
    out = tmp_path / "install.sh"
    code = build_installer.main([
        "--out", str(out), "--token", TOKEN, "--api-id", "12345", "--api-hash", API_HASH.upper(),
        "--env-file", str(tmp_path / "missing.env"),
    ])
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert "__" + "BOT_TOKEN__" not in text and f"BOT_TOKEN='{TOKEN}'" in text
    assert f"TG_API_HASH='{API_HASH}'" in text  # normalised to lower case
    assert "https://github.com/Mark5065559/video-re-editor.git" in text
    assert len(text.encode("utf-8")) < build_installer.PASTE_LIMIT_BYTES
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    if shutil.which("bash"):
        subprocess.run(["bash", "-n", str(out)], check=True)


def test_values_can_come_from_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"BOT_TOKEN={TOKEN}\nTG_API_ID=777\nTG_API_HASH={API_HASH}\n", encoding="utf-8")
    out = tmp_path / "install.sh"
    assert build_installer.main(["--out", str(out), "--env-file", str(env_file)]) == 0
    assert "TG_API_ID='777'" in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("token,api_id,api_hash,repo,branches", [
    ("bad", "1", API_HASH, build_installer.DEFAULT_REPO, "main"),
    (TOKEN, "1", "", build_installer.DEFAULT_REPO, "main"),
    (TOKEN, "12a", API_HASH, build_installer.DEFAULT_REPO, "main"),
    (TOKEN, "1", "xyz", build_installer.DEFAULT_REPO, "main"),
    (TOKEN, "1", API_HASH, "https://x.y/repo.git; rm -rf /", "main"),
    (TOKEN, "1", API_HASH, build_installer.DEFAULT_REPO, "main'; reboot"),
])
def test_bad_values_are_rejected(token, api_id, api_hash, repo, branches):
    with pytest.raises(ValueError):
        build_installer.build(token, api_id, api_hash, repo, branches)
