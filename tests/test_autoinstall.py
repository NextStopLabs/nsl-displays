import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sh(p):
    return os.path.join(REPO, p)


def test_shell_syntax():
    for p in ("pi/autoinstall/firstrun.sh", "pi/image/prepare-golden.sh",
              "pi/image/nsl-golden-firstboot.sh", "pi/image/nsl-set-unit.sh",
              "pi/image/capture.sh"):
        assert os.path.exists(sh(p)), p
        r = subprocess.run(["bash", "-n", sh(p)], capture_output=True, text=True)
        assert r.returncode == 0, f"{p}: {r.stderr}"


def test_firstrun_defaults_and_safety():
    s = open(sh("pi/autoinstall/firstrun.sh")).read()
    assert 'NSL_ROLE="front"' in s
    assert "https://nsldisplays.com/portal" in s
    assert "exit 0" in s  # never break first boot
    assert "nsl-autoinstall.service" in s
    assert "authorized_keys" in s  # optional nsl-ssh.pub support
    assert "chage -d 0 pi" in s  # force password change from default
    # installer dispatched by role, same scripts as manual setup
    assert "install-controller.sh" in s and "install-board.sh" in s


def test_golden_flow_files():
    svc = open(sh("pi/image/nsl-golden-firstboot.service")).read()
    assert "ConditionPathExists=!/var/lib/nsl/.image-booted" in svc
    fb = open(sh("pi/image/nsl-golden-firstboot.sh")).read()
    assert "device-token" in fb and "pairing.json" in fb
    assert "ssh-keygen -A" in fb and "systemd-machine-id-setup" in fb
    assert "NSL_TOKEN/d" in fb  # token stripped from drop-in, portal URL kept
    prep = open(sh("pi/image/prepare-golden.sh")).read()
    assert "device-token" in prep and "REFUSING" in prep  # must be unpaired
    cap = open(sh("pi/image/capture.sh")).read()
    assert "refusing" in cap
