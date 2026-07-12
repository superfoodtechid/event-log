"""
core/browser.py
===============
Handles Shopee Partner login via Selenium and session token persistence.

Flow:
1. Try to load a saved session from `data/session.json`.
2. Validate the saved token against the Shopee API (lightweight call).
3. If valid → use tokens directly (no browser needed).
4. If invalid/missing → open browser, login, extract tokens, save to file.
5. After login, navigate to the business-hours-settings page which triggers
   Shopee to issue the shopee_tob_token cookie.
"""

import os
import json
import time
import random
from datetime import datetime
from pathlib import Path

import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from core.logger import get_logger


log = get_logger("browser")

# ── Constants ──────────────────────────────────────────────────────────────────
SESSION_FILE    = Path(__file__).resolve().parent.parent / "data" / "session.json"
import sys
import threading
from pathlib import Path

# Stub function since Discord notifications are not needed
def send_discord_error(*args, **kwargs):
    pass

_thread_local = threading.local()

def get_session_file() -> Path:
    if not hasattr(_thread_local, "session_file"):
        _thread_local.session_file = Path(__file__).resolve().parent.parent / "data" / "session.json"
    return _thread_local.session_file

def get_otp_code(username: str, phone: str) -> str:
    discord_mode = os.getenv("OFD_DISCORD_MODE") == "1"
    if not discord_mode:
        if not sys.stdin.isatty():
            log.warning("⚠️ [OTP] Stdin is not a TTY (running in background/Docker). Cannot prompt for OTP via terminal. Waiting 10 seconds...")
            time.sleep(10)
            return ""
        try:
            return input(f"🔑 Masukkan 6-digit OTP (atau tekan Enter jika Anda mengisinya langsung di browser): ").strip()
        except EOFError:
            log.warning("⚠️ [OTP] Stdin reached EOF. Waiting 10 seconds...")
            time.sleep(10)
            return ""

    
    script_dir = Path(__file__).resolve().parent.parent
    data_dir = script_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    otp_file = data_dir / f"otp_request_{username}.json"
    
    request_data = {
        "status": "WAITING_OTP",
        "username": username,
        "phone": phone,
        "requested_at": datetime.now().isoformat()
    }
    
    try:
        otp_file.write_text(json.dumps(request_data, indent=2))
        print(f"DISCORD_OTP_REQUEST: {json.dumps(request_data)}", flush=True)
        log.info(f"Sent OTP request to Discord for: {username}")
    except Exception as e:
        log.error(f"Gagal menulis file request OTP: {e}")
        return ""
    
    log.info(f"⏳ [DISCORD] Menunggu input OTP dari Discord untuk akun {username}...")
    
    start_wait = time.time()
    while time.time() - start_wait < 86400:
        if otp_file.exists():
            try:
                data = json.loads(otp_file.read_text())
                if data.get("status") == "RECEIVED" and data.get("code"):
                    otp_code = str(data["code"]).strip()
                    log.info(f"✅ [DISCORD] OTP diterima dari Discord: {otp_code}")
                    otp_file.unlink(missing_ok=True)
                    return otp_code
            except Exception as e:
                log.error(f"Error membaca file OTP: {e}")
        time.sleep(2)
        
    log.warning(f"❌ [DISCORD] Timeout menunggu OTP untuk {username}")
    otp_file.unlink(missing_ok=True)
    return ""

def set_session_file(val):
    _thread_local.session_file = Path(val)

class ThreadLocalSessionFileProxy:
    def __getattr__(self, name):
        return getattr(get_session_file(), name)
        
    def __str__(self):
        return str(get_session_file())
        
    def __fspath__(self):
        return str(get_session_file())

    def __eq__(self, other):
        return get_session_file() == other

SESSION_FILE = ThreadLocalSessionFileProxy()

# Wrap the module class to intercept external writes to SESSION_FILE
class ModuleWrapper(sys.modules[__name__].__class__):
    @property
    def SESSION_FILE(self):
        return get_session_file()
        
    @SESSION_FILE.setter
    def SESSION_FILE(self, value):
        set_session_file(value)

sys.modules[__name__].__class__ = ModuleWrapper
PARTNER_DASHBOARD    = "https://partner.shopee.co.id/food/dashboard"
TOKEN_TRIGGER_PAGE   = "https://partner.shopee.co.id/settings/shopee-food/business-hours-settings"
MERCHANT_SELECTOR_URL = "https://partner.shopee.co.id/food/dashboard"  # ALIASED to dashboard to prevent redirect to merchant-selector
VALIDATE_URL         = "https://api.partner.shopee.co.id/nb/mss/web-api/PartnerAccountServer/GetUserInfo"
SHOPEE_IMG_BASE      = "https://down-id.img.susercontent.com/file"

# Words that must NEVER be clicked — guard against accidental logout
LOGOUT_KEYWORDS = ["log out", "logout", "keluar", "sign out", "signout"]


# ── Helpers ────────────────────────────────────────────────────────────────────

def human_like_typing(element, text: str):
    # Direct input is faster; using it as requested
    element.send_keys(text)

def _is_safe_to_click(element) -> bool:
    """Returns False if the element text matches a logout/exit keyword."""
    try:
        text = (element.text or "").strip().lower()
        if not text:
            text = (element.get_attribute("innerText") or "").strip().lower()
        return not any(kw in text for kw in LOGOUT_KEYWORDS)
    except Exception:
        return True  # Assume safe if text cannot be read

def _detect_and_recover_logout(driver) -> bool:
    """
    Safety-net: detects if the browser accidentally got logged out.
    Attempts re-entry using the existing Chrome profile cookies (no OTP needed).
    Returns True if recovery succeeded, False otherwise.
    """
    current = driver.current_url.lower()
    logged_out = (
        "/login" in current
        or "/authenticate/login" in current
        or "about:blank" in current
    )
    if not logged_out:
        return False  # Not logged out — nothing to do

    log.warning("⚠️  [LOGOUT-RECOVERY] Accidental logout detected! Trying to recover via Chrome profile...")
    try:
        driver.get(PARTNER_DASHBOARD)
        time.sleep(5)
        recovered_url = driver.current_url.lower()
        if "dashboard" in recovered_url or "merchant-selector" in recovered_url:
            log.info("✅ [LOGOUT-RECOVERY] Recovered without OTP — Chrome profile cookies still valid.")
            return True
    except Exception as err:
        log.warning(f"⚠️  [LOGOUT-RECOVERY] Recovery attempt failed: {err}")

    log.warning("⚠️  [LOGOUT-RECOVERY] Could not recover automatically — full re-login may be needed.")
    return False

def _handle_onboarding_invitation(driver, timeout=15) -> bool:
    """
    Detects and handles the Shopee Partner onboarding INVITATION page.

    This page appears when a new merchant invitation is pending. It shows
    "Gabung dengan Merchant Baru" with a single "Gabung dengan Merchant" button.
    Unlike the merchant selector/list page, there is NO list of merchants here.

    Returns True if invitation was accepted (or at least clicked), False if
    the page is not an invitation page.
    """
    try:
        current_url = driver.current_url.lower()
        if "onboarding" not in current_url:
            return False

        # Distinguish invitation page (has Gabung button, NO merchant list)
        # from merchant selector page (has .listItem elements)
        page_info = driver.execute_script("""
            var allButtons = Array.from(document.querySelectorAll('button'));
            var gabungBtn = null;
            for (var btn of allButtons) {
                var text = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                if (text.includes('gabung')) { gabungBtn = btn; break; }
            }
            var hasListItems = document.querySelectorAll(
                '.listItem, .merchant-item, li[class*="item"]'
            ).length > 0;
            return { hasGabung: !!gabungBtn, hasList: hasListItems };
        """)

        if not page_info or not page_info.get("hasGabung") or page_info.get("hasList"):
            return False

        log.info("📍 [ONBOARDING] Merchant invitation page detected. Clicking 'Gabung dengan Merchant'...")

        btn_xpath = "//button[contains(., 'Gabung dengan Merchant') or contains(., 'Gabung')]"
        gabung_btn = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, btn_xpath))
        )
        gabung_btn.click()
        log.info("  👉 Clicked 'Gabung dengan Merchant' button.")
        time.sleep(3)

        # Wait for redirect away from the invitation page
        for _ in range(20):
            new_url = driver.current_url.lower()
            if "/food/dashboard" in new_url:
                log.info("  ✅ [ONBOARDING] Invitation accepted → Dashboard loaded.")
                return True
            if new_url != current_url:
                log.info(f"  ✅ [ONBOARDING] Invitation accepted → Redirected to: {driver.current_url}")
                return True
            time.sleep(1)

        # Button was clicked but no redirect detected — still consider it handled
        log.warning("  ⚠️ [ONBOARDING] Gabung clicked but no redirect detected within 20s.")
        return True

    except Exception as e:
        log.warning(f"  ⚠️ [ONBOARDING] Failed to handle invitation page: {e}")
        return False

def _deliberate_logout_and_relogin(
    driver,
    username: str = None,
    password: str = None,
    phone:    str = None,
) -> bool:
    """
    Intentional recovery strategy for when merchant cannot be detected.

    Flow:
      1. Click the profile area  →  open dropdown
      2. Click 'Log Out' from the dropdown
      3. Click the confirmation 'Log Out' button
      4. Try Chrome profile auto-login (fast path, no OTP)
      5. Fallback: enter credentials (username/password) via _perform_login()
      Returns True if back on the portal, False on complete failure.
    """
    log.info("🔄 [LOGOUT-RELOGIN] Initiating deliberate logout for clean session recovery...")
    try:
        # Check if already on login/authenticate page (meaning we are redirected or logged out already)
        url_now = driver.current_url.lower()
        if "login" in url_now or "authenticate" in url_now:
            log.info("  🛡️ Browser is already on the login/authenticate page. Skipping UI dropdown logout.")
            log.info("  🌐 Attempting direct login preserving all cookies/storage to leverage device trust...")
            if not (username and password) and not phone:
                log.warning("  ⚠️ No credentials provided — cannot complete login.")
                return False
            wait = WebDriverWait(driver, 30)
            login_ok = _perform_login(driver, wait, username=username, password=password, phone=phone)
            if login_ok:
                log.info("  ⏳ Menunggu pengalihan halaman setelah login recovery...")
                redirected_ok = False
                for _ in range(30):  # 30 * 0.5s = 15s max wait
                    curr_url = driver.current_url.lower()
                    if "onboarding" in curr_url or "merchant-selector" in curr_url or "dashboard" in curr_url:
                        redirected_ok = True
                        break
                    time.sleep(0.5)
                if redirected_ok:
                    log.info("  ✅ [LOGOUT-RELOGIN] Credential login succeeded directly from login page!")
                    return True
            return False

        # ── Step 1: Navigate to a page that has the profile dropdown ───
        if "/food/" not in driver.current_url and "/settings/" not in driver.current_url:
            driver.get(PARTNER_DASHBOARD)
            time.sleep(3)

        # ── Step 2: Open the profile/merchantName dropdown with retries ───
        profile_clicked = False
        for attempt in range(3):
            # Dismiss any blocking overlays/notifications
            driver.execute_script("""
                document.querySelectorAll('.ant-notification, .ant-modal, .ant-notification-notice, .ant-message').forEach(el => el.remove());
            """)
            
            # Find the WebElement via JS returning it
            profile_el = driver.execute_script("""
                var profileEl = null;
                // 1. Try specific CSS selectors first
                for (var sel of ['.merchantName', '.user-info', '.ant-dropdown-trigger', '.ant-dropdown-link']) {
                    var el = document.querySelector(sel);
                    if (el && el.offsetHeight > 0) {
                        profileEl = el;
                        break;
                    }
                }
                // 2. Search for element containing "Admin:"
                if (!profileEl) {
                    var elements = Array.from(document.querySelectorAll('span, p, div, li, a'));
                    for (var el of elements) {
                        var text = (el.innerText || '').trim();
                        if (text.includes('Admin:') && text.length < 30 && el.offsetHeight > 0) {
                            profileEl = el;
                            break;
                        }
                    }
                }
                // 3. Fallback to last .ant-dropdown-trigger
                if (!profileEl) {
                    var triggers = Array.from(document.querySelectorAll('.ant-dropdown-trigger, .ant-dropdown-link'));
                    if (triggers.length > 0) {
                        profileEl = triggers[triggers.length - 1];
                    }
                }
                return profileEl;
            """)
            
            if profile_el:
                log.info(f"  📍 Found profile menu element (Attempt {attempt+1}). Dispatching JS click...")
                # Dispatch JS events
                driver.execute_script("""
                    var el = arguments[0];
                    var ev1 = new MouseEvent('mouseover', { bubbles: true, cancelable: true });
                    var ev2 = new MouseEvent('mouseenter', { bubbles: true, cancelable: true });
                    var ev3 = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
                    var ev4 = new MouseEvent('click', { bubbles: true, cancelable: true });
                    var ev5 = new MouseEvent('mouseup', { bubbles: true, cancelable: true });
                    el.dispatchEvent(ev1);
                    el.dispatchEvent(ev2);
                    el.dispatchEvent(ev3);
                    el.dispatchEvent(ev4);
                    el.dispatchEvent(ev5);
                """, profile_el)
                time.sleep(1.5)
                
                # Check if dropdown is visible (ignoring hidden parents)
                has_dropdown = driver.execute_script("""
                    var targets = ['log out', 'logout', 'keluar'];
                    var candidates = Array.from(document.querySelectorAll('li, span, div, a'));
                    for (var el of candidates) {
                        var rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        if (el.closest('.ant-dropdown-hidden, [style*="display: none"], [style*="visibility: hidden"]')) continue;
                        var text = (el.innerText || '').trim().toLowerCase();
                        if (targets.some(function(k){ return text.includes(k); })) {
                            return true;
                        }
                    }
                    return false;
                """)
                
                if not has_dropdown:
                    log.info("  ⚠️ JS click did not reveal dropdown. Retrying with Selenium native ActionChains hover/click...")
                    try:
                        actions = ActionChains(driver)
                        actions.move_to_element(profile_el).perform()
                        time.sleep(0.5)
                        actions.click(profile_el).perform()
                        time.sleep(1.5)
                        
                        has_dropdown = driver.execute_script("""
                            var targets = ['log out', 'logout', 'keluar'];
                            var candidates = Array.from(document.querySelectorAll('li, span, div, a'));
                            for (var el of candidates) {
                                var rect = el.getBoundingClientRect();
                                if (rect.width === 0 || rect.height === 0) continue;
                                if (el.closest('.ant-dropdown-hidden, [style*="display: none"], [style*="visibility: hidden"]')) continue;
                                var text = (el.innerText || '').trim().toLowerCase();
                                if (targets.some(function(k){ return text.includes(k); })) {
                                    return true;
                                }
                            }
                            return false;
                        """)
                    except Exception as e:
                        log.warning(f"  ⚠️ ActionChains failed: {e}")
                
                if has_dropdown:
                    log.info("  ✅ Dropdown is now visible.")
                    profile_clicked = True
                    break
                else:
                    log.warning("  ⚠️ Dropdown menu elements not visible yet. Retrying...")
            else:
                log.warning(f"  ⚠️ Profile element not found on page (Attempt {attempt+1}). Retrying...")
            time.sleep(1.5)

        if not profile_clicked:
            log.warning("  ⚠️ Profile element or dropdown could not be opened.")
            return False

        # ── Step 3: Find and click 'Log Out' in the dropdown ────────────
        logout_el = driver.execute_script("""
            var targets = ['log out', 'logout', 'keluar'];
            var candidates = Array.from(document.querySelectorAll(
                'li.ant-menu-item, li[role="menuitem"], .ant-dropdown-menu-item,'
                + '[class*="menu-item"], span, div, a'
            ));
            for (var el of candidates) {
                var rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) continue;
                if (el.closest('.ant-dropdown-hidden, [style*="display: none"], [style*="visibility: hidden"]')) continue;
                
                var text = (el.innerText || '').trim().toLowerCase();
                if (targets.some(function(k){ return text === k; })) {
                    // Walk up to the closest interactive wrapper (e.g. li or .ant-dropdown-menu-item)
                    var clickable = el.closest('li, button, a, [role="menuitem"], .ant-dropdown-menu-item') || el;
                    return clickable;
                }
            }
            return null;
        """)

        if not logout_el:
            log.warning("  ⚠️ 'Log Out' menu item not found in dropdown.")
            return False

        # Click it using Selenium
        try:
            log.info("  👈 Clicking 'Log Out' menu item...")
            logout_el.click()
        except Exception:
            # Fallback to ActionChains
            try:
                ActionChains(driver).move_to_element(logout_el).click().perform()
            except Exception as e:
                log.warning(f"  ⚠️ Selenium click failed: {e}. Trying JS MouseEvents as fallback...")
                driver.execute_script("""
                    var el = arguments[0];
                    var ev1 = new MouseEvent('mouseover', { bubbles: true, cancelable: true });
                    var ev2 = new MouseEvent('mouseenter', { bubbles: true, cancelable: true });
                    var ev3 = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
                    var ev4 = new MouseEvent('click', { bubbles: true, cancelable: true });
                    var ev5 = new MouseEvent('mouseup', { bubbles: true, cancelable: true });
                    el.dispatchEvent(ev1); el.dispatchEvent(ev2); el.dispatchEvent(ev3); el.dispatchEvent(ev4); el.dispatchEvent(ev5);
                """, logout_el)
        
        time.sleep(1.5)  # Wait for confirmation dialog

        # ── Step 4: Click the 'Log Out' confirmation button with retries ────
        confirm_clicked = False
        for confirm_attempt in range(5):
            confirm_el = driver.execute_script("""
                var targets = ['log out', 'logout', 'keluar'];
                // ONLY look inside modal containers
                var modal = document.querySelector('.ant-modal-content, .ant-modal, .ant-dialog, .ant-modal-wrap');
                if (!modal) return null;
                
                var candidates = Array.from(modal.querySelectorAll('button, .ant-btn, [role="button"]'));
                for (var btn of candidates) {
                    var rect = btn.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    var text = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                    if (targets.some(function(k){ return text === k || text === ('confirm ' + k); })) {
                        // Walk up to the closest clickable element (e.g. button or .ant-btn)
                        var clickable = btn.closest('button, [role="button"], a, .ant-btn') || btn;
                        return clickable;
                    }
                }
                return null;
            """)
            
            if confirm_el:
                log.info(f"  📍 Found confirmation button on Attempt {confirm_attempt+1}. Clicking...")
                try:
                    confirm_el.click()
                except Exception as e:
                    log.warning(f"  ⚠️ Selenium click failed: {e}. Trying ActionChains...")
                    try:
                        ActionChains(driver).move_to_element(confirm_el).click().perform()
                    except Exception as e2:
                        log.warning(f"  ⚠️ ActionChains click failed: {e2}. Trying JS click...")
                        driver.execute_script("arguments[0].click();", confirm_el)
                
                time.sleep(2)
                # Verify if modal is gone
                modal_present = driver.execute_script("""
                    var modal = document.querySelector('.ant-modal-content, .ant-modal, .ant-dialog, .ant-modal-wrap');
                    return !!(modal && modal.offsetHeight > 0);
                """)
                if not modal_present:
                    log.info("  ✅ Modal disappeared. Logout confirmed.")
                    confirm_clicked = True
                    break
                else:
                    log.warning("  ⚠️ Modal is still present after click. Retrying...")
            else:
                log.warning(f"  ⚠️ Confirmation button/modal not found yet (Attempt {confirm_attempt+1}). Retrying...")
                time.sleep(1.5)

        if not confirm_clicked:
            log.warning("  ⚠️ Confirmation 'Log Out' button could not be clicked via UI.")
            
            # --- DEBUG SCREENSHOT JIKA KLIK GAGAL ---
            try:
                import os
                debug_dir = os.path.join("src", "shopee-omzet-automation", "data", "debug")
                os.makedirs(debug_dir, exist_ok=True)
                ss_fail_path = os.path.join(debug_dir, "modal_fail_server.png")
                driver.save_screenshot(ss_fail_path)
                log.info(f"  📸 [DEBUG] Screenshot penyebab kegagalan klik disimpan di {ss_fail_path}")
            except Exception as e:
                pass
            # ----------------------------------------
            
            log.warning("  ⚠️ UI logout failed. Aborting recovery to prevent manual cookie deletion and OTP.")
            return False

        log.info("  ✅ Logout confirmed. Waiting for login page...")
        time.sleep(3)

        # ── Step 5a: Try Chrome profile auto-login (fast path) ──────────
        log.info("  🌐 Attempting Chrome profile auto-login...")
        driver.get(PARTNER_DASHBOARD)
        time.sleep(5)
        url_now = driver.current_url.lower()
        if "dashboard" in url_now or "merchant-selector" in url_now or "onboarding" in url_now:
            log.info("  ✅ [LOGOUT-RELOGIN] Auto-login via Chrome profile succeeded!")
            return True

        # ── Step 5b: Fallback — login dengan kredensial ────────────────
        log.info("  ⚠️ Chrome profile auto-login failed — logging in with credentials...")
        if not (username and password) and not phone:
            log.warning("  ⚠️ No credentials provided — cannot complete login.")
            return False

        # Navigate to login page if not already there
        current = driver.current_url.lower()
        if "login" not in current and "authenticate" not in current:
            driver.get("https://partner.shopee.co.id/login")
            time.sleep(4)

        wait = WebDriverWait(driver, 30)
        login_ok = _perform_login(driver, wait, username=username, password=password, phone=phone)
        if not login_ok:
            log.error("  ❌ Credential login failed.")
            return False

        # Wait for dashboard or merchant selector after login
        time.sleep(3)
        url_after = driver.current_url.lower()
        if "dashboard" in url_after or "merchant-selector" in url_after or "onboarding" in url_after:
            log.info("  ✅ [LOGOUT-RELOGIN] Credential login succeeded!")
            return True

        # Handle merchant-selector page if redirected there post-login
        for _ in range(10):
            url_after = driver.current_url.lower()
            if "dashboard" in url_after or "merchant-selector" in url_after or "onboarding" in url_after:
                log.info("  ✅ [LOGOUT-RELOGIN] Logged in and on portal.")
                return True
            time.sleep(1)

        log.warning(f"  ⚠️ [LOGOUT-RELOGIN] Unexpected URL after credential login: {driver.current_url}")
        return False

    except Exception as e:
        log.error(f"  ❌ [LOGOUT-RELOGIN] Failed: {e}")
        return False

def build_img_url(img_id: str) -> str:
    if not img_id: return ""
    return f"{SHOPEE_IMG_BASE}/{img_id}"


# ── Session Persistence ────────────────────────────────────────────────────────

def save_session(tob_token: str, entity_id: str, extra_cookies: dict = None):
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "shopee_tob_token": tob_token,
        "shopee_tob_entity_id": entity_id,
        "saved_at": datetime.now().isoformat(),
        "extra_cookies": extra_cookies or {},
    }
    SESSION_FILE.write_text(json.dumps(payload, indent=2))
    log.debug(f"✅ Session saved to {SESSION_FILE}")

def load_session() -> dict | None:
    if not SESSION_FILE.exists(): return None
    try:
        data = json.loads(SESSION_FILE.read_text())
        if data.get("shopee_tob_token"):
            log.info(f"📂 [SESSION] Found cached session (saved at {data.get('saved_at')})")
            return data
    except: pass
    return None

def validate_session(tob_token: str, entity_id: str) -> bool:
    log.debug("🔍 Validating saved session token...")
    headers = {
        "Cookie": f"shopee_tob_entity_id={entity_id}; shopee_tob_token={tob_token}",
        "x-merchant-token": tob_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    try:
        resp = requests.post(VALIDATE_URL, json={}, headers=headers, timeout=8)
        data = resp.json()
        if data.get("message") == "success" or data.get("code") == 0:
            log.info("✅ [SESSION] Saved session is still valid.")
            return True
    except: pass
    return False


# ── Token Extraction ───────────────────────────────────────────────────────────

def extract_tokens_from_driver(driver) -> tuple:
    tob_token = None
    entity_id = None
    for c in driver.get_cookies():
        name = c["name"]
        val = c["value"]
        if name == "shopee_tob_token": 
            tob_token = val
        elif name.lower() in ["shopee_tob_entity_id", "shopee_foody_mid", "x-merchant-id", "spc_merchant_id", "merchant_id", "shopid", "shop_id"]:
            if val and not entity_id: entity_id = val
            
    if not entity_id:
        try: 
            # Try API first (Most accurate) - using full URL and credentials
            api_js = """
            var token = arguments[0];
            var done = arguments[1];
            fetch('https://api.partner.shopee.co.id/nb/mss/web-api/PartnerAccountServer/GetUserInfo', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'x-merchant-token': token || ''
                },
                body: '{}',
                credentials: 'include'
            })
            .then(r => r.json())
            .then(j => done(j.data ? j.data.merchantId : null))
            .catch(() => done(null));
            """
            token = ""
            for c in driver.get_cookies():
                if c["name"] == "shopee_tob_token":
                    token = c["value"]
                    break
            entity_id = driver.execute_async_script(api_js, token)
        except: pass

    if not entity_id:
        try: 
            entity_id = driver.execute_script("""
                let ids = [];
                // 1. Check all numeric storage values
                for (let i = 0; i < localStorage.length; i++) {
                    let k = localStorage.key(i);
                    let v = localStorage.getItem(k);
                    if (/^\\d{6,12}$/.test(v)) ids.push(v);
                }
                // ... rest of fallback logic ...
                let specific = localStorage.getItem('shopee_tob_entity_id') || 
                               localStorage.getItem('shopee_foody_mid') || 
                               localStorage.getItem('merchant_id') || 
                               localStorage.getItem('spc_merchant_id');
                if (specific) return specific;
                return ids[0] || null;
            """)
        except: pass
    
    return tob_token, (str(entity_id).strip() if entity_id else None)

def get_all_cookies_dict(driver) -> dict:
    return {c["name"]: c["value"] for c in driver.get_cookies()}

def _trigger_and_extract_tokens(driver) -> tuple:
    log.debug("  🔄 Triggering fresh token issuance...")
    try:
        try: driver.delete_cookie("shopee_tob_token")
        except: pass
        driver.get(TOKEN_TRIGGER_PAGE)
        for _ in range(10):
            tob_token, entity_id = extract_tokens_from_driver(driver)
            if tob_token: return tob_token, entity_id
            time.sleep(1)
    except: pass
    return extract_tokens_from_driver(driver)


# ── Driver Initialization ──────────────────────────────────────────────────────

def _init_driver(headless: bool):
    options = Options()
    options.add_argument("--log-level=3")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-component-update")
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")
    else:
        options.add_argument("--start-maximized")
    
    script_dir = Path(__file__).parent.parent
    if SESSION_FILE.stem == "session":
        profile_dir = script_dir / "data" / "chrome_profile"
        options.add_argument(f"--user-data-dir={profile_dir.resolve()}")
        options.add_argument("--profile-directory=shopee_profile")
    else:
        account_name = SESSION_FILE.stem.replace("session_", "")
        profile_dir = script_dir / "data" / f"chrome_profile_{account_name}"
        options.add_argument(f"--user-data-dir={profile_dir.resolve()}")
        options.add_argument(f"--profile-directory=profile_{account_name}")

    # Delete SingletonLock if it exists to avoid SessionNotCreatedException on Linux
    singleton_lock = profile_dir / "SingletonLock"
    if singleton_lock.exists() or singleton_lock.is_symlink():
        try:
            singleton_lock.unlink(missing_ok=True)
            log.info(f"🧹 Removed Chrome SingletonLock at {singleton_lock}")
        except Exception as e:
            log.warning(f"⚠️ Failed to remove SingletonLock: {e}")

    try:
        # Use native Selenium Manager (faster, more stable, avoids ChromeDriverManager network hangs)
        driver = webdriver.Chrome(options=options)
    except Exception as e:
        log.warning(f"⚠️ Native Chrome init failed: {e}. Trying ChromeDriverManager fallback...")
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    driver.set_page_load_timeout(60)
    return driver


# ── Login Logic ────────────────────────────────────────────────────────────────

def _perform_login(driver, wait, username: str = None, password: str = None, phone: str = None, is_retry: bool = False) -> bool:
    log.info("➡️  [AUTH] Starting login sequence...")
    if not phone and (not username or not password):
        raise Exception("Shopee credentials are not configured! Please configure them in 'credentials.json' at the project root directory.")
    
    use_phone = phone and not (username and password)
    if use_phone:
        try:
            wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), 'Log in dengan no. HP')]"))).click()
            time.sleep(1)
        except: pass
        phone_input = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "input[type='tel']")))
        phone_input.send_keys(Keys.CONTROL + "a", Keys.BACKSPACE)
        human_like_typing(phone_input, phone)
        wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Selanjutnya')]"))).click()
    else:
        # Wait for page to stabilize
        time.sleep(2)
        
        # Robust selectors for login fields
        user_input = None
        # Try finding ANY visible text input first
        try:
            inputs = driver.find_elements(By.CSS_SELECTOR, "input")
            for inp in inputs:
                p = (inp.get_attribute("placeholder") or "").lower()
                n = (inp.get_attribute("name") or "").lower()
                t = (inp.get_attribute("type") or "").lower()
                if inp.is_displayed() and (t == "text" or "user" in n or "phone" in n or "handphone" in p or "username" in p):
                    user_input = inp
                    break
        except: pass

        if not user_input:
            # Last ditch attempt with specific selectors
            for sel in ["input[name='userName']", "input[placeholder*='handphone']", "input[placeholder*='Username']", "input[type='text']"]:
                try:
                    el = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, sel)))
                    if el.is_displayed(): user_input = el; break
                except: continue
        
        if not user_input:
            log.error(f"❌ Failed to find Username field. URL: {driver.current_url}")
            # Log all input attributes for debugging
            try:
                all_inps = driver.find_elements(By.TAG_NAME, "input")
                log.debug(f"  Found {len(all_inps)} input tags on page.")
                for i, el in enumerate(all_inps):
                    log.debug(f"    [{i}] name={el.get_attribute('name')} type={el.get_attribute('type')} placeholder={el.get_attribute('placeholder')} visible={el.is_displayed()}")
            except: pass
            raise Exception("Could not find Username input field")

        pass_input = None
        for sel in ["input[type='password']", "input[placeholder='Password']"]:
            try:
                el = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, sel)))
                if el.is_displayed(): pass_input = el; break
            except: continue
            
        if not pass_input: raise Exception("Could not find Password input field")

        user_input.send_keys(Keys.CONTROL + "a", Keys.BACKSPACE)
        human_like_typing(user_input, username)
        pass_input.send_keys(Keys.CONTROL + "a", Keys.BACKSPACE)
        human_like_typing(pass_input, password)
        
        # Click login button
        login_btn = None
        for btn_sel in ["//button[contains(., 'Masuk') or contains(., 'Log In')]", "//button[@type='submit']"]:
            try:
                btn = wait.until(EC.element_to_be_clickable((By.XPATH, btn_sel)))
                if btn.is_displayed(): login_btn = btn; break
            except: continue

        if login_btn: login_btn.click()
        else: raise Exception("Could not find Login button")

    # Check for immediate credential errors
    time.sleep(3)
    try:
        error_texts = driver.execute_script("""
            var errs = Array.from(document.querySelectorAll('.shopee-form-item__error-message, .shopee-alert__title, .ant-message-custom-content span'));
            return errs.map(e => e.innerText).filter(t => t.length > 0);
        """)
        for err_text in error_texts:
            if "sandi" in err_text.lower() or "password" in err_text.lower() or "salah" in err_text.lower() or "nomor" in err_text.lower() or "username" in err_text.lower():
                log.error(f"❌ Login error detected: {err_text}")
                if is_retry:
                    send_discord_error("Shopee", username or phone, "WRONG_CREDENTIALS", f"Gagal login: {err_text}", phone)
                return False
            if "blokir" in err_text.lower() or "blocked" in err_text.lower() or "dibatasi" in err_text.lower():
                log.error(f"❌ Account block detected: {err_text}")
                if is_retry:
                    send_discord_error("Shopee", username or phone, "BLOCKED_ACCOUNT", f"Akun dibatasi/diblokir: {err_text}", phone)
                return False
    except: pass

    log.debug("  ⏳ Waiting for post-login redirect or OTP...")
    start_wait = time.time()
    while time.time() - start_wait < 30:
        current_url = driver.current_url.lower()
        if "onboarding" in current_url or "merchant-selector" in current_url or "dashboard" in current_url:
            break
        try:
            # Check for any OTP input
            otp_input = None
            for sel in ["input.shopee-otp-input__input", ".shopee-otp-input input", "input[maxlength='6']"]:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    if el.is_displayed(): otp_input = el; break
                if otp_input: break

            # Or check for verification page elements/texts
            is_verification_page = driver.execute_script("""
                var texts = [
                    "pilih cara verifikasi", "select verification method",
                    "pilih metode verifikasi", "verify to log in",
                    "verifikasi untuk masuk", "masukkan kode", "enter code",
                    "kode verifikasi", "verification code"
                ];
                var bodyText = (document.body.innerText || "").toLowerCase();
                return texts.some(function(t) { return bodyText.includes(t); });
            """)

            if otp_input or is_verification_page:
                log.error(f"❌ [AUTH] OTP or verification is required for '{username or phone}'. Aborting to prevent triggering OTP.")
                return False
        except Exception:
            pass

        # Cek dan klik tombol Lanjutkan/Continue jika ada di halaman konfirmasi setelah login
        try:
            btn_el = driver.find_element(By.XPATH, "//button[contains(., 'Lanjutkan') or contains(., 'Continue')] | //*[text()='Lanjutkan' or text()='Continue']")
            if btn_el.is_displayed():
                log.info("👉 [AUTH] Menemukan tombol 'Lanjutkan', mencoba mengklik...")
                try:
                    btn_el.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", btn_el)
                time.sleep(2)
        except Exception:
            pass

        time.sleep(1)

    # Re-verify that we successfully navigated away from login/authenticate pages
    current_url = driver.current_url.lower()
    if "onboarding" not in current_url and "merchant-selector" not in current_url and "dashboard" not in current_url:
        log.error(f"❌ [AUTH] Login did not redirect to dashboard and is still on: {current_url}. Aborting.")
        return False

    return True

def auto_switch_merchant(driver, target_name, is_retry=False):
    """
    Automated merchant switch using the profile menu dropdown on the dashboard.
    This avoids the selector page which often triggers forced re-logins.
    """
    log.info(f"🔄 [MERCHANT] Switching to: {target_name}...")
    try:
        # Check if already on the target merchant
        try:
            current_name = driver.find_element(By.CSS_SELECTOR, ".merchantName").text.strip()
            if current_name.lower().strip().rstrip('.') == target_name.lower().strip().rstrip('.'):
                log.info(f"✅ [MERCHANT] Already on target merchant: {current_name}. No switch needed.")
                return True
        except Exception as e:
            pass

        # 0. Fast Loader Removal
        driver.execute_script("document.querySelectorAll('.ant-spin, [class*=\"loading\"], .shopee-loading').forEach(el => el.remove());")
        
        wait = WebDriverWait(driver, 15)

        js_selector_click = """
            // PHASE 1: Selalu klik merchant pertama di list untuk bypass selector page.
            // Tujuannya hanya keluar dari halaman ini — PHASE 2 yang handle switch ke target.
            // Merchant baru (yang perlu di-accept) selalu muncul di posisi paling atas.
            var listItems = document.querySelectorAll('.listItem, .merchant-item, li[class*="item"]');
            for (var i = 0; i < listItems.length; i++) {
                var el = listItems[i];
                var text = (el.innerText || el.textContent || "").trim();
                if (text.length > 0) {
                    el.scrollIntoView({block: 'center'});
                    el.click();
                    return true;
                }
            }
            return false;
        """

        # PHASE 1: Handle initial merchant selector page right after login
        current_url = driver.current_url
        if "onboarding" in current_url or "merchant-selector" in current_url:
            log.debug(f"  📍 Detected Merchant Selector page (URL: {current_url}). Attempting to bypass...")
            time.sleep(3)
            
            for attempt in range(5):
                if driver.execute_script(js_selector_click):
                    log.debug(f"  ✅ Triggered selection on selector page. Waiting for dashboard or invitation...")
                    try:
                        # After clicking a merchant in the selector list, Shopee can:
                        #   a) Redirect to /food/dashboard (existing/accepted merchant)
                        #   b) Show invitation page with "Gabung" button (new merchant)
                        # The invitation page may have the SAME base URL (SPA) or a different one.
                        # We check for: URL change, dashboard URL, OR "Gabung" button appearing.
                        pre_click_url = driver.current_url
                        
                        def _page_transitioned(d):
                            cur = d.current_url
                            if "/food/dashboard" in cur:
                                return True
                            if cur != pre_click_url:
                                return True
                            # SPA case: URL unchanged but invitation content loaded
                            try:
                                btns = d.find_elements(By.XPATH,
                                    "//button[contains(., 'Gabung dengan Merchant') or contains(., 'Gabung')]")
                                if any(b.is_displayed() for b in btns):
                                    return True
                            except:
                                pass
                            return False
                        
                        WebDriverWait(driver, 30).until(_page_transitioned)
                        time.sleep(3)
                        
                        # If we landed on an onboarding invitation page, accept it
                        if "/food/dashboard" not in driver.current_url:
                            if _handle_onboarding_invitation(driver):
                                time.sleep(3)
                        
                        # Re-check current name after landing on dashboard
                        if "/food/dashboard" in driver.current_url:
                            try:
                                actual_name = driver.find_element(By.CSS_SELECTOR, ".merchantName").text.strip().lower()
                                if target_name.lower() in actual_name:
                                    return True
                                else:
                                    log.info(f"  📍 Landed on dashboard as '{actual_name}'. Will switch to target now.")
                                    break 
                            except:
                                break
                    except: pass
                # Scroll if not found
                driver.execute_script("window.scrollBy(0, 300);")
                time.sleep(1)
            
            if "onboarding" in driver.current_url or "merchant-selector" in driver.current_url:
                raise Exception(f"Failed to bypass Merchant Selector page")

        # PHASE 2: Dashboard Switch Logic
        if "/food/dashboard" not in driver.current_url:
            driver.get(PARTNER_DASHBOARD)
            time.sleep(2)
        
        for switch_attempt in range(3):
            # Use ActionChains to hover Profile then "Pilih Merchant Lain"
            dropdown_opened = False
            try:
                actions = ActionChains(driver)
                # 1. Hover/Click merchantName (Profile)
                profile_menu = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".merchantName")))
                actions.move_to_element(profile_menu).click().perform()
                time.sleep(1)
                
                # 2. Cek apakah dropdown terbuka dengan timeout SINGKAT (3 detik)
                #    Jika 3 detik tidak muncul, sesi kemungkinan stale/overlay menghalangi.
                quick_wait = WebDriverWait(driver, 3)
                try:
                    switch_trigger = quick_wait.until(EC.presence_of_element_located((By.XPATH, "//span[contains(text(), 'Pilih Merchant Lain') or contains(text(), 'Switch Merchant')]")))
                    # Harus di-click agar sub-menu daftar merchant muncul dengan benar (tidak sekadar hover)
                    actions.move_to_element(switch_trigger).click().perform()
                    dropdown_opened = True
                    time.sleep(1)
                except:
                    # Fallback: coba JS click dan CEK hasilnya
                    js_found = driver.execute_script("""
                        var spans = document.querySelectorAll('span, p, div');
                        for (var s of spans) {
                            var text = (s.innerText || '').trim();
                            if (text.includes('Pilih Merchant Lain') || text.includes('Switch Merchant')) {
                                s.click();
                                return true;
                            }
                        }
                        return false;
                    """)
                    if js_found:
                        dropdown_opened = True
                        time.sleep(1)
            except Exception as e:
                err_str = str(e)
                log.warning(f"  ⚠️ Failed to trigger merchant menu: {err_str}")
                # Jika elemen .merchantName sama sekali tidak ditemukan (Timeout), 
                # kemungkinan besar sesi sudah logout atau halaman corrupt. Fail fast!
                if "TimeoutException" in err_str or "merchantName" not in driver.page_source:
                    log.warning("  ⚠️ [STALE SESSION] Elemen profil (.merchantName) tidak ditemukan. Sesi kemungkinan kedaluwarsa.")
                    return False
                    
                if switch_attempt == 2:
                    return False
                continue

            # DETEKSI SESI STALE: Jika dropdown tidak terbuka, sesi sudah kedaluwarsa.
            # Langsung return False agar pipeline memicu recovery (logout + login ulang).
            if not dropdown_opened:
                log.warning(f"  ⚠️ [STALE SESSION] Dropdown profil tidak terbuka setelah klik — sesi kemungkinan kedaluwarsa.")
                return False

            # Use JS to click the target merchant in the revealed list
            js_switch_script = """
                var targetName = arguments[0].toLowerCase().trim();
                var items = document.querySelectorAll('li.ant-menu-item, li[role="menuitem"], .ant-dropdown-menu-item, [class*="menu-item"]');
                for (var i = 0; i < items.length; i++) {
                    var text = (items[i].innerText || "").toLowerCase().trim();
                    if (text === targetName || text.includes(targetName)) {
                        items[i].scrollIntoView({block: 'center'});
                        items[i].click();
                        return true;
                    }
                }
                return false;
            """
            
            found_target = False
            # Polling selama 5 detik dengan SCROLL untuk memastikan seluruh daftar termuat (Lazy Load)
            for _ in range(5):
                if driver.execute_script(js_switch_script, target_name):
                    found_target = True
                    break
                # Scroll ke bawah di dalam elemen dropdown/list untuk memuat sisa merchant
                try:
                    driver.execute_script("document.querySelectorAll('.ant-dropdown-menu, ul[role=\"menu\"], .ant-popover-inner-content').forEach(el => el.scrollTop += 600);")
                except: pass
                time.sleep(1)
                
            if found_target:
                log.debug(f"  ✅ Clicked {target_name} in menu.")
            else:
                log.warning(f"  ⚠️ Nama outlet '{target_name}' tidak ditemukan di dropdown (Attempt {switch_attempt+1}/3).")
                if switch_attempt == 2:
                    msg = f"Nama outlet '{target_name}' tidak terdaftar atau belum ditambahkan (invite) di akun Shopee ini."
                    log.error(f"❌ {msg}")
                    # Mengirimkan error ke Discord secara langsung karena ini fatal dan kita akan langsung abort.
                    send_discord_error(
                        platform="Shopee", 
                        merchant=target_name, 
                        error_type="SYSTEM_ERROR", 
                        message=msg
                    )
                    # Lempar error spesifik agar pipeline terluar menangkapnya
                    raise ValueError(f"MERCHANT_NOT_FOUND: {target_name}")
                continue # Ulangi proses klik profil dan buka dropdown dari awal

            # Wait to see if we redirect to onboarding invitation page
            time.sleep(3)
            current_url = driver.current_url.lower()
            if "onboarding" in current_url:
                log.info("📍 [MERCHANT] Onboarding page detected after selecting merchant. Accepting invitation...")
                if _handle_onboarding_invitation(driver):
                    log.info("  ✅ Invitation accepted via helper.")
                    time.sleep(3)
                    # After accepting, wait for dashboard if not already there
                    if "/food/dashboard" not in driver.current_url:
                        try:
                            WebDriverWait(driver, 15).until(lambda d: "/food/dashboard" in d.current_url)
                        except:
                            pass
                else:
                    log.error("❌ Failed to accept onboarding invitation.")
                    if switch_attempt == 2:
                        return False
                    continue

            # Cek apakah nama merchant di UI berubah dalam 5 detik (Sesuai instruksi User)
            try:
                log.info(f"  ⏳ Menunggu 5 detik melihat pembaruan nama menjadi {target_name} (Attempt {switch_attempt+1}/3)...")
                def is_name_updated(d):
                    try:
                        return target_name.lower() in d.find_element(By.CSS_SELECTOR, ".merchantName").text.lower()
                    except:
                        return False
                        
                WebDriverWait(driver, 5).until(is_name_updated)
                log.info(f"✅ [MERCHANT] Switched to: {target_name}")
                return True
            except:
                log.warning(f"⚠️ [MERCHANT] UI name belum berubah ke {target_name}.")
                if switch_attempt == 2:
                    log.warning(f"❌ [MERCHANT] Gagal melakukan switch ke {target_name} setelah 3x percobaan klik.")
                    if is_retry:
                        send_discord_error(
                            platform="Shopee", 
                            merchant=target_name, 
                            error_type="SYSTEM_ERROR", 
                            message=f"Dashboard tidak memuat profil outlet '{target_name}' meskipun sudah 3x dipilih di menu."
                        )
                    return False
                # Jika belum attempt terakhir, loop akan berputar dan mengulang klik dari awal
    except Exception as e:
        if "MERCHANT_NOT_FOUND" in str(e):
            raise e
        log.error(f"❌ Auto-switch failed: {e}")
        return False





def _handle_merchant_selection(driver, active_id_forced=None, interactive=True):
    log.info("===========================================================================")
    """
    Handles merchant selection, either automatically if a target is known 
    or interactively if needed.
    """
    try:
        # Get active ID robustly
        active_id = active_id_forced
        if not active_id:
            _, active_id = extract_tokens_from_driver(driver)
            
        if active_id:
            log.info(f"📍 [MERCHANT] Active ID: {active_id}")
        
        # Try to find all merchants for interactive selection
        all_found = {}
        all_merchants_data = {}
        try:
            api_response_path = Path(__file__).resolve().parent.parent / "API" / "response.json"
            if api_response_path.exists():
                with open(api_response_path, "r") as f:
                    data = json.load(f)
                    for m in data.get("data", {}).get("selectMerchant", {}).get("merchantList", []):
                        all_merchants_data[m["merchantName"].lower()] = str(m["merchantId"])
        except: pass

        target_names = list(all_merchants_data.keys())
        
        # Robust & 1vCPU friendly JS scan for merchant list
        for attempt in range(10):
            log.debug(f"  📥 Scanning for merchants (Attempt {attempt+1}/10)...")
            scan_result = driver.execute_script("""
                var results = [];
                // Target specific merchant-like containers to avoid querying thousands of nodes
                var items = document.querySelectorAll('.listItem, .merchant-item, li[class*="item"], li, [class*="merchant"], [class*="shop"]');
                for (var i = 0; i < items.length; i++) {
                    var el = items[i];
                    // Skip wrappers with many children to target leaf nodes/cards
                    if (el.children.length > 3) continue;
                    var text = (el.innerText || "").trim().split('\\n')[0];
                    if (!text || text.length < 3 || text.length > 50) continue;
                    
                    // Exclude generic non-merchant phrases inside JS to save CPU
                    var name_key = text.toLowerCase();
                    var generic = [
                        "akun", "pengaturan", "log out", "halaman utama", "baru", "menu", "outlet", 
                        "shopeefood", "terapkan", "sembunyikan", "notifikasi", "pilih merchant lain", 
                        "pusat bantuan", "transaksi berhasil", "baris per halaman", "ringkasan toko", 
                        "nama toko", "jumlah total", "laporan saya", "penghasilan", "performa outlet", 
                        "periode transaksi", "ubah bahasa", "daftar merchant", "daftar di sini", 
                        "memulai bisnis baru?", "pilih merchant", "gabung dengan merchant", 
                        "buat merchant baru", "hubungi kami", "faq", "syarat & ketentuan",
                        "pusat edukasi seller"
                    ];
                    if (generic.some(g => name_key === g || name_key.includes(g))) continue;

                    let rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        results.push({ name: text, index: i });
                    }
                }
                return results;
            """)

            if scan_result:
                all_els = driver.find_elements(By.CSS_SELECTOR, '.listItem, .merchant-item, li[class*="item"], li, [class*="merchant"], [class*="shop"]')
                for r in scan_result:
                    name = r['name']
                    name_key = name.lower()
                    m_id = all_merchants_data.get(name_key) or "Unknown"
                    
                    # Jika kita punya data API (all_merchants_data tidak kosong), 
                    # HANYA masukkan merchant yang ID-nya dikenali (valid).
                    if all_merchants_data and m_id == "Unknown":
                        continue
                        
                    # Filter out obvious non-merchant generic texts
                    generic_texts = [
                        "akun", "pengaturan", "log out", "halaman utama", "baru", "menu", "outlet", 
                        "shopeefood", "terapkan", "sembunyikan", "notifikasi", "pilih merchant lain", 
                        "pusat bantuan", "transaksi berhasil", "baris per halaman", "ringkasan toko", 
                        "nama toko", "jumlah total", "laporan saya", "penghasilan", "performa outlet", 
                        "periode transaksi", "ubah bahasa", "daftar merchant", "daftar di sini", 
                        "memulai bisnis baru?", "pilih merchant", "gabung dengan merchant", 
                        "buat merchant baru", "hubungi kami", "faq", "syarat & ketentuan", 
                        "pusat edukasi seller"
                    ]
                    if m_id == "Unknown" and (len(name) < 4 or any(g == name_key or g in name_key for g in generic_texts) or "diupdate pada" in name_key):
                        continue
                        
                    if m_id != active_id and name not in all_found:
                        all_found[name] = {"name": name, "element": all_els[r['index']], "id": m_id}
            
            if len(all_found) >= 20: break
            # Try to scroll the list container
            driver.execute_script("document.querySelectorAll('div[class*=\"menu\"], ul[class*=\"menu\"], .ant-popover-content').forEach(el => el.scrollTop += 300);")
            time.sleep(1.5)

        # Do NOT sort alphabetically! Keep original DOM layout order (first merchant visible = index 1)
        merchants = list(all_found.values())
        if not merchants:
            if "/food/dashboard" in driver.current_url: return True
            log.warning("⚠️ No merchants found in scan.")
            return False

        print("\n" + "="*75 + f"\n  DAFTAR MERCHANT ({len(merchants)} ditemukan):\n" + "="*75)
        for i, m in enumerate(merchants, 1):
            print(f"  {i:2}. {m['name']} (ID: {m['id']})")
            
        if interactive:
            choice = input(f"\nPilih nomor (1-{len(merchants)}) atau Enter untuk lanjut: ").strip()
        else:
            log.info("⏭️  [MERCHANT] Mode otomatis (tanpa timeout), memilih secara otomatis...")
            if "/food/dashboard" not in driver.current_url:
                matched_idx = None
                
                # 1. Coba cocokkan dengan active_id_forced
                if active_id_forced:
                    for i, m in enumerate(merchants):
                        if str(m["id"]) == str(active_id_forced):
                            matched_idx = i + 1
                            break
                            
                if matched_idx:
                    log.info(f"👉 Ditemukan indeks merchant yang cocok: {matched_idx} ({merchants[matched_idx-1]['name']})")
                    choice = str(matched_idx)
                else:
                    log.info("👉 [MERCHANT] Onboarding/Selector page detected. Automatically choosing the first merchant to proceed.")
                    choice = "1"
            else:
                choice = ""
            
        if not choice: return True
        
        idx = int(choice)-1
        if 0 <= idx < len(merchants):
            sel = merchants[idx]
            log.info(f"👉 Memilih: {sel['name']}")
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", sel["element"])
            time.sleep(0.5)
            try: sel["element"].click()
            except: driver.execute_script("arguments[0].click();", sel["element"])
            
            log.info("  ⏳ Waiting for dashboard redirect...")
            WebDriverWait(driver, 30).until(EC.url_contains("/food/dashboard"))
            time.sleep(2)
            return True
        return False
    except Exception as e:
        log.error(f"Selection error: {e}")
        return False



def return_to_selector(driver) -> bool:
    """
    Navigates to the merchant selection interface.

    Strategy (safe order):
      1. Hover .merchantName to open the profile dropdown.
      2. Scan visible dropdown items with a LOGOUT_KEYWORDS blacklist.
      3. Click the first item that contains 'Pilih Merchant' / 'Switch Merchant'.
      4. If not found safely → fall back to direct URL navigation.

    ⚠️  We NEVER do a blind element click after opening the dropdown because
         doing so has been observed to trigger the 'Log Out' button and cause
         an accidental logout (confirmed bug, 2026-06-04).
    """
    log.debug("🔄 Opening merchant selector via UI menu (safe mode)...")
    try:
        # Ensure we are on a page where the profile menu exists
        if "/food/dashboard" not in driver.current_url:
            driver.get(PARTNER_DASHBOARD)
            time.sleep(3)

        wait    = WebDriverWait(driver, 10)
        actions = ActionChains(driver)

        # ── Step 1: Locate the profile / merchant-name element ──────────────
        profile_menu = None
        for sel in [".merchantName", ".user-info", "li.ant-menu-item:last-child"]:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    profile_menu = el
                    break
            except Exception:
                continue

        if not profile_menu:
            log.warning("⚠️ Profile menu not found — using direct URL fallback.")
            driver.get(MERCHANT_SELECTOR_URL)
            return True

        # ── Step 2: Hover to open the dropdown ─────────────────────────────
        try:
            actions.move_to_element(profile_menu).perform()
            time.sleep(1)  # Let the dropdown render
        except Exception:
            pass

        # ── Step 3: Scan dropdown items with blacklist guard ────────────────
        # Use JS to read all visible menu items and find 'Pilih Merchant Lain'
        safe_click_done = driver.execute_script("""
            var keywords = ['pilih merchant', 'switch merchant', 'ganti merchant'];
            var blacklist = ['log out', 'logout', 'keluar', 'sign out'];

            // Gather all potentially clickable items currently visible
            var candidates = Array.from(document.querySelectorAll(
                'li.ant-menu-item, li[role="menuitem"], .ant-dropdown-menu-item, '
                + '[class*="menu-item"], span, div, a'
            ));

            for (var el of candidates) {
                var rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) continue;

                var text = (el.innerText || '').trim().toLowerCase();
                if (!text) continue;

                // ⛔ NEVER click logout-related elements
                if (blacklist.some(function(k){ return text.includes(k); })) continue;

                // ✅ Click if it's a 'switch merchant' action
                if (keywords.some(function(k){ return text.includes(k); })) {
                    el.click();
                    return true;
                }
            }
            return false;
        """)

        if safe_click_done:
            log.debug("  ✅ Clicked 'Pilih Merchant Lain' safely via JS scan.")
            time.sleep(2)
            return True

        # ── Step 4: Safe fallback — direct URL (no UI interaction risk) ─────
        log.warning("  ⚠️ 'Pilih Merchant Lain' not found in dropdown — using direct URL fallback.")
        driver.get(MERCHANT_SELECTOR_URL)
        time.sleep(3)
        return True

    except Exception as e:
        log.error(f"❌ return_to_selector failed: {e} — falling back to direct URL.")
        try:
            driver.get(MERCHANT_SELECTOR_URL)
        except Exception:
            pass
        return True

def get_session(username=None, password=None, phone=None, headless=True, close_browser=True, target_name=None, interactive=True) -> dict | None:
    for attempt in range(3):
        log.info(f"🌐 [BROWSER] Launching (headless={headless}, attempt={attempt+1}/3)...")
        driver = _init_driver(headless=headless)
        wait = WebDriverWait(driver, 30)
        session_success = False

        try:
            # ── Step 1: Check browser state first (Profile session) ──
            driver.get(PARTNER_DASHBOARD)
            time.sleep(4)
            
            is_logged_in = False
            current_url = driver.current_url.lower()
            
            # Check if already logged in (on any attempt)
            # Note: "onboarding" pages also indicate a valid session (pending merchant invitation)
            if "dashboard" in current_url or "merchant-selector" in current_url or "onboarding" in current_url:
                log.info("✅ [SESSION] Browser is already logged in.")
                is_logged_in = True
            
            # Restore from file only on first attempt if not logged in
            if not is_logged_in and attempt == 0:
                saved = load_session()
                if saved:
                    log.debug("🔍 Attempting to restore session from saved tokens...")
                    driver.add_cookie({"name": "shopee_tob_token", "value": saved["shopee_tob_token"]})
                    if saved.get("shopee_tob_entity_id"):
                        driver.add_cookie({"name": "shopee_tob_entity_id", "value": saved["shopee_tob_entity_id"]})
                    for n, v in saved.get("extra_cookies", {}).items():
                        try: driver.add_cookie({"name": n, "value": v})
                        except: pass
                    
                    driver.refresh()
                    time.sleep(4)
                    current_url = driver.current_url.lower()
                    if "dashboard" in current_url or "merchant-selector" in current_url:
                        log.info("✅ [SESSION] Restored from saved tokens.")
                        is_logged_in = True

            # On retry attempts, try injecting saved session tokens BEFORE resorting
            # to a full fresh login. Chrome may have crashed mid-session (causing
            # "Connection refused") but the session_{username}.json written by the
            # previous successful warm cycle is still valid. Injecting those cookies
            # into a fresh Chrome instance avoids triggering Shopee OTP.
            if not is_logged_in and attempt > 0:
                log.info(f"🔄 [SESSION] Attempt {attempt+1}: trying saved tokens before fresh login...")
                saved = load_session()
                if saved and saved.get("shopee_tob_token"):
                    try:
                        driver.add_cookie({"name": "shopee_tob_token", "value": saved["shopee_tob_token"]})
                        if saved.get("shopee_tob_entity_id"):
                            driver.add_cookie({"name": "shopee_tob_entity_id", "value": saved["shopee_tob_entity_id"]})
                        for n, v in saved.get("extra_cookies", {}).items():
                            try: driver.add_cookie({"name": n, "value": v})
                            except: pass
                        driver.refresh()
                        time.sleep(4)
                        current_url = driver.current_url.lower()
                        if "dashboard" in current_url or "merchant-selector" in current_url:
                            log.info(f"✅ [SESSION] Restored from saved tokens on retry {attempt+1} — no fresh login needed.")
                            is_logged_in = True
                    except Exception as _cookie_err:
                        log.warning(f"  ⚠️ Cookie injection on retry failed: {_cookie_err}")



            # ── Step 3: Login if all above failed ──
            if not is_logged_in:
                log.info("⚠️ [SESSION] No active session. Navigating to login...")
                if "/login" not in driver.current_url.lower() and "authenticate" not in driver.current_url.lower():
                    driver.get("https://partner.shopee.co.id/login")
                    time.sleep(5)
                
                current_url = driver.current_url.lower()
                if "login" in current_url or "authenticate" in current_url or "about:blank" in current_url:
                    success = _perform_login(driver, wait, username, password, phone, is_retry=(attempt == 2))
                    if not success:
                        log.error("❌ [AUTH] _perform_login failed.")
                        driver.quit()
                        continue
                    
                # Wait dynamically for either dashboard, onboarding, or merchant-selector URL (up to 15s)
                log.info("  ⏳ Menunggu pengalihan halaman setelah login...")
                redirected_ok = False
                for _ in range(30):  # 30 * 0.5s = 15s max wait
                    curr_url = driver.current_url.lower()
                    if "onboarding" in curr_url or "merchant-selector" in curr_url or "dashboard" in curr_url:
                        redirected_ok = True
                        break
                    time.sleep(0.5)

                if redirected_ok and ("onboarding" in driver.current_url.lower() or "merchant-selector" in driver.current_url.lower()):
                    log.info("📍 [SESSION] Detected Onboarding page. Checking page type...")
                    bypass_success = False
                    
                    # First: check if this is a merchant invitation page ("Gabung" button, no list)
                    if _handle_onboarding_invitation(driver):
                        time.sleep(3)
                        if "/food/dashboard" in driver.current_url:
                            log.info("  ✅ [SESSION] Invitation accepted during session init. Continuing...")
                            bypass_success = True
                        # If still on onboarding/selector, fall through to listItem bypass below
                    
                    if not bypass_success:
                        log.info("📍 [SESSION] Merchant selector detected. Selecting first available merchant...")
                        bypass_js = """
                            var loaders = document.querySelectorAll('.ant-spin, [class*="loading"], .shopee-loading, .ant-spin-nested-loading');
                            loaders.forEach(el => el.remove());
                            // Klik elemen wrapper .listItem (bukan inner .merchantInfo) 
                            // karena event onClick menempel di wrapper terluar.
                            var target = document.querySelector('.listItem, .merchant-item, li[class*="item"], [class*="merchant-item"], .ant-list-item');
                            if (target) {
                                target.scrollIntoView({block: 'center'});
                                try { target.click(); } catch(e) {}
                                var clickEvent = new MouseEvent('click', {
                                    bubbles: true,
                                    cancelable: true,
                                    view: window
                                });
                                target.dispatchEvent(clickEvent);
                                return true;
                            }
                            return false;
                        """
                        for _ in range(10):
                            if driver.execute_script(bypass_js):
                                log.debug("  ✅ Selection triggered via JS.")
                                try:
                                    # Wait for either dashboard to load, onboarding page to load, or the join button to appear
                                    log.debug("  ⏳ Waiting for redirect (either dashboard or onboarding)...")
                                    start_redirect_wait = time.time()
                                    redirected = False
                                    is_onboard_route = False
                                    
                                    while time.time() - start_redirect_wait < 15:
                                        curr_url = driver.current_url.lower()
                                        if "/food/dashboard" in curr_url:
                                            redirected = True
                                            break
                                        if "onboarding" in curr_url:
                                            is_onboard_route = True
                                            redirected = True
                                            break
                                        # Check if the "Gabung" button is present on the page (even if URL hasn't changed yet)
                                        try:
                                            btns = driver.find_elements(By.XPATH, "//button[contains(., 'Gabung dengan Merchant') or contains(., 'Gabung') or contains(text(), 'Gabung')]")
                                            if any(b.is_displayed() for b in btns):
                                                is_onboard_route = True
                                                redirected = True
                                                break
                                        except: pass
                                        time.sleep(0.5)
                                    
                                    if is_onboard_route:
                                        log.info("📍 [SESSION] Onboarding page/modal detected. Accepting invitation...")
                                        try:
                                            btn_xpath = "//button[contains(., 'Gabung dengan Merchant') or contains(., 'Gabung') or contains(text(), 'Gabung')]"
                                            onboard_btn = WebDriverWait(driver, 10).until(
                                                EC.element_to_be_clickable((By.XPATH, btn_xpath))
                                            )
                                            onboard_btn.click()
                                            log.info("  👉 Clicked 'Gabung' button during session init onboarding")
                                            time.sleep(5)
                                        except Exception as err:
                                            log.warning(f"  ⚠️ Could not click Gabung button: {err}")
                                    
                                    # Finally, wait for the dashboard redirection to complete
                                    wait.until(lambda d: "/food/dashboard" in d.current_url)
                                    log.debug("  ✅ Landed on dashboard.")
                                    bypass_success = True
                                    break
                                except Exception as e:
                                    log.warning(f"  ⚠️ Onboarding selector bypass attempt failed: {e}")
                            try:
                                container = driver.find_element(By.CSS_SELECTOR, ".ant-list-items, [role='list']")
                                driver.execute_script("arguments[0].scrollTop += 300;", container)
                            except: pass
                            time.sleep(1)
                    if bypass_success: time.sleep(2)
            
            # ── Step 4: Extract current ID & Name via API ──
            log.debug("🔍 Fetching active merchant info via API...")
            active_id = None
            active_name = "Unknown Merchant"
            try:
                token = ""
                for c in driver.get_cookies():
                    if c["name"] == "shopee_tob_token":
                        token = c["value"]
                        break
                api_js = """
                var token = arguments[0];
                var done = arguments[1];
                fetch('https://api.partner.shopee.co.id/nb/mss/web-api/PartnerAccountServer/GetUserInfo', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'x-merchant-token': token || '',
                        'x-merchant-language': 'id',
                        'x-merchant-login-from': '12'
                    },
                    body: '{}',
                    credentials: 'include'
                })
                .then(r => r.json())
                .then(j => done(j.data || null))
                .catch(() => done(null));
                """
                driver.set_script_timeout(10)
                user_data = driver.execute_async_script(api_js, token)
                if user_data:
                    active_id = str(user_data.get("merchantId") or "")
                    active_name = user_data.get("merchantName") or "Unknown Merchant"
            except: pass

            # ── Step 4.5: Fallback to UI Name Matching ──
            if not active_id or active_id == "None":
                try:
                    log.debug("  ⏳ Menunggu sinkronisasi UI merchant (Maks 10 detik)...")
                    def get_ui_name(d):
                        try:
                            t = d.find_element(By.CLASS_NAME, "merchantName").text.strip()
                            return t if t else False
                        except:
                            return False
                            
                    ui_name = WebDriverWait(driver, 10).until(get_ui_name)
                    if ui_name:
                        active_name = ui_name
                        api_response_path = Path(__file__).resolve().parent.parent / "API" / "response.json"
                        with open(api_response_path, "r") as f:
                            m_data = json.load(f)
                            for m in m_data.get("data", {}).get("selectMerchant", {}).get("merchantList", []):
                                if m["merchantName"].lower() == ui_name.lower():
                                    active_id = str(m["merchantId"])
                                    log.info(f"📍 [MERCHANT] Detected UI: {active_name} (ID: {active_id})")
                                    break
                except Exception as e:
                    pass

            if not active_id:
                _, active_id = extract_tokens_from_driver(driver)
            
            # ── Step 5: Decision - Switch or Stay? ──
            do_switch = False
            if target_name:
                # Normalize names (strip whitespace and trailing dots) for robust comparison
                norm_active = active_name.lower().strip().rstrip('.')
                norm_target = target_name.lower().strip().rstrip('.')
                if norm_active != norm_target:
                    log.info(f"📍 [MERCHANT] Current: {active_name} | Target: {target_name}. Switching...")
                    do_switch = True
                elif not active_id or active_id == "None":
                    log.info(f"⚠️ [MERCHANT] Target is {active_name}, but active_id is missing! Forcing switch to hydrate session cookies...")
                    do_switch = True
                else:
                    log.info(f"✅ [MERCHANT] Already as target: {active_name}")
            else:
                is_invalid_name = (
                    not active_name or
                    active_name.lower().strip() == "unknown merchant" or
                    active_name.lower().strip() == "admin"
                )
                if active_id and active_id != "None" and not is_invalid_name:
                    log.info(f"📍 [MERCHANT] Current: {active_name} (ID: {active_id})")
                    do_switch = False
                else:
                    log.info(f"📍 [MERCHANT] Invalid active merchant detected (Name: {active_name}, ID: {active_id}). Redirecting/Switching...")
                    do_switch = True

            if do_switch:
                if target_name:
                    success = auto_switch_merchant(driver, target_name, is_retry=(attempt == 2))
                    if not success:
                        log.warning(f"⚠️ [MERCHANT] auto_switch_merchant failed for target {target_name}. Initiating logout/relogin recovery...")
                        recovered = _deliberate_logout_and_relogin(
                            driver,
                            username=username,
                            password=password,
                            phone=phone,
                        )
                        if recovered:
                            log.info("🔄 [MERCHANT] Recovery successful. Retrying merchant switch...")
                            success = auto_switch_merchant(driver, target_name, is_retry=(attempt == 2))
                        else:
                            log.error("❌ Recovery failed.")
                            success = False
                else:
                    # When merchant cannot be detected, do a deliberate logout + relogin
                    # via the Chrome profile. This gives a clean session state without OTP:
                    #   1. Click profile → select 'Log Out' from dropdown
                    #   2. Confirm logout
                    #   3. Chrome profile auto-logs back in (no OTP)
                    log.info("🔄 [MERCHANT] Unknown/Admin/Missing merchant — initiating logout/relogin recovery...")
                    recovered = _deliberate_logout_and_relogin(
                        driver,
                        username=username,
                        password=password,
                        phone=phone,
                    )
                    if recovered:
                        # After re-entry, run merchant selection normally
                        success = _handle_merchant_selection(driver, active_id_forced=None, interactive=interactive)
                    else:
                        log.error("❌ Logout/relogin recovery failed. Cannot proceed.")
                        success = False
                if not success:
                    log.error("❌ Merchant selection failed.")
                    driver.quit()
                    continue
            else:
                if "/food/dashboard" not in driver.current_url:
                    driver.get(PARTNER_DASHBOARD)
                    time.sleep(2)

            # ── Step 6: Final Token Extraction ──
            t, eid = _trigger_and_extract_tokens(driver)
            if not eid and active_id and active_id != "None":
                log.info(f"⚠️ [SESSION] Token extraction returned empty entity_id. Using fallback active_id: {active_id}")
                eid = active_id
                
            if not t:
                log.warning("⚠️ Token extraction failed.")
                driver.quit()
                continue
                
            all_c = get_all_cookies_dict(driver)
            save_session(t, eid or "", extra_cookies=all_c)
            res = {"shopee_tob_token": t, "shopee_tob_entity_id": eid or "", "extra_cookies": all_c}
            if not close_browser: res["driver"] = driver
            session_success = True
            return res

        except Exception as e:
            err_msg = str(e)
            log.error(f"Browser session error on attempt {attempt+1}: {err_msg}")
            # Jika errornya adalah merchant tidak ditemukan, tidak ada gunanya login ulang 3x. Langsung abort.
            if "MERCHANT_NOT_FOUND" in err_msg:
                log.error("❌ Fatal Error: Merchant belum ditambahkan. Membatalkan antrean tanpa login ulang.")
                raise e
        finally:
            if (close_browser or not session_success) and driver is not None:
                try: driver.quit()
                except: pass

    log.error("❌ Max login retries reached.")
    return None

def refresh_tokens(driver, fallback_entity_id=None) -> dict:
    t, eid = _trigger_and_extract_tokens(driver)
    if not eid and fallback_entity_id and fallback_entity_id != "None":
        log.info(f"⚠️ [SESSION] refresh_tokens: Using fallback_entity_id: {fallback_entity_id}")
        eid = fallback_entity_id
    all_c = get_all_cookies_dict(driver)
    save_session(t, eid or "", extra_cookies=all_c)
    return {"shopee_tob_token": t, "shopee_tob_entity_id": eid or "", "extra_cookies": all_c}

