"""Real-browser E2E: click through the portal UI like a user. Needs playwright chromium."""
import os
import subprocess
import sys
import time
import urllib.request
import wave

import pytest

pytestmark = pytest.mark.e2e

PORT = 8135
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture()
def server(tmp_path):
    env = dict(os.environ, DATABASE_URL=f"sqlite:{tmp_path}/dev.db", STORAGE_DIR=str(tmp_path / "storage"))
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "portal.backend.app:app",
                          "--host", "127.0.0.1", "--port", str(PORT)], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=".")
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(BASE + "/health", timeout=2)
                break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError("server did not start")
        yield BASE
    finally:
        p.terminate()
        p.wait(timeout=10)


def test_full_user_journey(server, tmp_path):
    from playwright.sync_api import expect, sync_playwright
    errors, failed = [], []
    shot = str(tmp_path / "shot")
    os.makedirs(shot, exist_ok=True)
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page()
        pg.on("dialog", lambda d: d.accept())
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: failed.append(m.text) if m.type == "error" else None)

        # 1. landing redirects to UI, asks to sign in
        pg.goto(BASE + "/", wait_until="networkidle")
        assert pg.url.endswith("/static/"), pg.url
        pg.screenshot(path=f"{shot}/01-auth.png")
        assert pg.locator("#authview").is_visible()

        # 2. bad login shows error (the 401 console noise below is expected)
        pg.fill("#si-email", "driver@bus.co")
        pg.fill("#si-pass", "wrong")
        pg.locator("#form-login button[type=submit]").click()
        pg.locator("#si-err:not(:empty)").wait_for(timeout=5000)
        del failed[:]

        # 3. signup page: mismatched passwords rejected, then account created
        pg.click("#link-signup")
        pg.locator("#auth-signup").wait_for()
        pg.fill("#su-name", "Driver")
        pg.fill("#su-org", "Bus Co")
        pg.fill("#su-email", "driver@bus.co")
        pg.fill("#su-pass", "secret123")
        pg.fill("#su-pass2", "different")
        pg.locator("#form-signup button[type=submit]").click()
        pg.locator("#su-err").get_by_text("do not match").wait_for(timeout=5000)
        pg.fill("#su-pass2", "secret123")
        pg.locator("#form-signup button[type=submit]").click()
        pg.locator("#mainview").wait_for(timeout=5000)
        assert "driver@bus.co" in (pg.text_content("#who") or "")
        pg.screenshot(path=f"{shot}/02-programs.png")

        # 4. new program + open
        pg.fill("#np-name", "E2E routes")
        pg.click("#btn-newprog")
        pg.get_by_text("E2E routes").first.wait_for()
        pg.locator("[data-open]").first.click()
        pg.locator("#progdetail").wait_for()

        # 5. add service (lives in the collapsed Services section)
        pg.locator("#svc-details > summary").click()
        pg.fill("#ns-num", "100")
        pg.fill("#ns-name", "Town loop")
        pg.click("#ns-add")
        pg.wait_for_function("document.querySelectorAll('#nd-service option').length > 1", timeout=10000)

        # 6. bad code flagged live, good code accepted (one form for the program)
        pg.fill("#nd-code", "123")
        pg.locator("text=Code must be exactly 8 digits").wait_for()
        pg.fill("#nd-code", "10000001")
        pg.fill("#nd-name", "Burton")
        pg.fill("#nd-via", "Branston")
        pg.fill("#nd-dest", "BURTON")
        pg.click("#nd-add")
        pg.get_by_text("10000001").first.wait_for()
        pg.fill("#nd-code", "10000002")
        pg.fill("#nd-name", "Lichfield")
        pg.fill("#nd-dest", "LICHFIELD")
        pg.click("#nd-add")
        pg.get_by_text("10000002").first.wait_for()
        pg.screenshot(path=f"{shot}/03-destinations.png")

        # 7. stops on both destinations: two stops, terminus last, save
        for idx in (0, 1):
            pg.locator("[data-stops]").nth(idx).click()
            pg.click("#stop-add")
            pg.click("#stop-add")
            rows = pg.locator("#stoprows tr")
            assert rows.count() == 2
            rows.nth(0).locator("[data-f=name]").fill(f"Stop{idx}A")
            rows.nth(1).locator("[data-f=name]").fill(f"Stop{idx}B")
            rows.nth(1).locator("[data-f=is_terminus]").check()
            pg.click("#stop-save")
            pg.get_by_text("10000001").first.wait_for()  # detail re-rendered

        # 8. visual layout editor: auto-fit starter, drag, save, reload
        pg.locator("[data-layout]").first.click()
        pg.locator("#lay-canvas").wait_for()
        pg.locator("#lay-status").get_by_text("0 element(s)").wait_for()
        pg.click("#lay-autofit")
        pg.locator("#lay-status").get_by_text("2 element(s)").wait_for()
        # canvas drag moves element #0 (x 0 -> ~10)
        pg.select_option("#lay-ellist", "0")
        box = pg.locator("#lay-canvas").bounding_box()
        x0 = pg.locator("#lay-props [data-p=x]").input_value()
        pg.mouse.move(box["x"] + 28 * 3, box["y"] + 20 * 3)
        pg.mouse.down()
        pg.mouse.move(box["x"] + 28 * 3 + 30, box["y"] + 20 * 3, steps=5)
        pg.mouse.up()
        pg.wait_for_function(
          f"document.querySelector('#lay-props [data-p=x]').value !== '{x0}'", timeout=5000)
        pg.click("#lay-save")  # alert auto-accepted
        # reload from server proves it persisted
        pg.select_option("#lay-disp", index=1)
        pg.select_option("#lay-disp", index=0)
        pg.locator("#lay-status").get_by_text("2 element(s)").wait_for()
        # 9. publish (assert it really published — no silent validation failure)
        pg.fill("#pd-notes", "first version")
        pg.click("#pd-publish")  # alert auto-accepted
        pg.locator("#pd-versions").get_by_text("v1").wait_for()
        pg.screenshot(path=f"{shot}/04-published.png")

        # 10. simulate device + pair
        pg.click("#tabs .tab[data-tab=controllers]")
        pg.click("#btn-sim")
        pg.fill("#pair-name", "bus-1")
        pg.click("#btn-pair")
        pg.locator("#token-dialog[open]").wait_for()  # show-once token dialog
        assert pg.locator("#token-value").input_value() != ""
        pg.screenshot(path=f"{shot}/04b-token.png")
        pg.click("#btn-token-close")
        pg.locator("#ctlrows").get_by_text("bus-1").wait_for()

        # 11. queue upload (reopen program so controller checkbox exists)
        pg.click("#tabs .tab[data-tab=programs]")
        pg.locator("[data-open]").first.click()
        pg.locator("#up-details > summary").click()
        pg.locator("#pd-ctls input").first.check()
        pg.click("#pd-upload")  # alert auto-accepted

        # 12. export downloads a pack
        with pg.expect_download() as dl:
            pg.locator("[data-exp]").first.click()
        path = dl.value.path()
        assert os.path.getsize(path) > 1000

        # 13. codes tab
        pg.click("#tabs .tab[data-tab=codes]")
        pg.click("#btn-codes")
        expect(pg.locator("#coderows tr")).to_have_count(2, timeout=10000)

        # 14. audio: generate + upload
        pg.click("#tabs .tab[data-tab=audio]")
        pg.fill("#gen-text", "The next stop is Depot")
        pg.fill("#gen-name", "next-depot")
        pg.click("#btn-generate")  # alert auto-accepted
        pg.get_by_text("next-depot").wait_for()
        wav = str(tmp_path / "tone.wav")
        with wave.open(wav, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
            import math as _m
            import struct as _s
            n = 2205
            w.writeframes(_s.pack(f"<{n}h", *[int(8000 * _m.sin(2 * _m.pi * 440 * i / 22050)) for i in range(n)]))
        pg.set_input_files("#au-file", wav)
        pg.click("#btn-upload")  # alert auto-accepted
        pg.screenshot(path=f"{shot}/05-audio.png")

        # 15. remote control
        pg.click("#tabs .tab[data-tab=remote]")
        pg.select_option("#rc-ctl", index=0)
        pg.click("#btn-rc-refresh")
        pg.locator("#rc-state:not(:empty)").wait_for()
        for btn in ("#btn-next", "#btn-prev", "#btn-an", "#btn-aa", "#btn-vol", "#btn-mute", "#btn-ovc"):
            pg.click(btn)
        pg.fill("#rc-ov", "NOT IN SERVICE")
        pg.click("#btn-ov")
        pg.fill("#rc-code", "10000001")
        pg.click("#btn-rc-code")
        assert "Command queued" in (pg.text_content("#rc-state") or "")
        pg.screenshot(path=f"{shot}/06-remote.png")

        # 16. sign out + back in, session gone then restored via login
        pg.click("#btn-logout")
        assert pg.locator("#authview").is_visible()
        pg.fill("#si-email", "driver@bus.co")
        pg.fill("#si-pass", "secret123")
        pg.locator("#form-login button[type=submit]").click()
        pg.locator("#mainview").wait_for(timeout=5000)
        b.close()
    assert not errors, errors
    assert not failed, failed
