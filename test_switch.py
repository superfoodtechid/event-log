#!/usr/bin/env python3
"""
test_switch.py
==============
Test script to open the Shopee Partner Dashboard, inject the network interceptor,
and perform a merchant switch to target: "SuperFood ." while capturing network traffic.

Usage:
    .venv/bin/python test_switch.py [account_name]
Example:
    .venv/bin/python test_switch.py superfoodapp
"""

import os
import sys
import time
import json
from pathlib import Path
from datetime import datetime

# ── Path Setup ─────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from core import browser
from capture_events import MONKEYPATCH_JS


def main():
    # Target merchant name requested by user
    target_merchant = "SuperFood"
    
    # Target account/session profile. Always use 'allvbadmin' to reuse active session cookies.
    account_name = "allvbadmin"
    
    print(f"🎯 Target Merchant: {target_merchant}")
    print(f"👤 Target Account Profile: {account_name}")
    
    # Configure the session file for the browser module
    session_file = SCRIPT_DIR / "data" / f"session_{account_name}.json"
    browser.set_session_file(session_file)
    
    print(f"📂 Session file configured to: {session_file}")
    
    # ── Step 1: Open Dashboard (No auto-switch inside get_session to allow injecting interceptor first) ──
    username = None
    password = None
    phone = None
    cred_file = SCRIPT_DIR / "credentials.json"
    if cred_file.exists():
        try:
            creds = json.loads(cred_file.read_text())
            username = creds.get("shopee_username")
            password = creds.get("shopee_password")
            phone = creds.get("shopee_phone")
        except Exception as e:
            print(f"⚠️ Error reading credentials.json: {e}")

    print("🌐 Launching Chrome and opening dashboard...")
    session_data = browser.get_session(
        username=username or None,
        password=password or None,
        phone=phone or None,
        headless=True,
        close_browser=False,
        interactive=True
    )
    
    if not session_data or "driver" not in session_data:
        print("❌ Failed to open browser session.")
        sys.exit(1)
        
    driver = session_data["driver"]
    print("✅ Dashboard opened successfully.")
    
    # ── Step 2: Open 2 additional windows (total 3 windows) ──
    dashboard_url = "https://partner.shopee.co.id/food/dashboard"
    store_ids = ["21708903", "21830864", "21708892"]
    base_url = "https://partner.shopee.co.id/settings/shopee-food/business-hours-settings/business-hours?storeId="
    
    # Navigasi Window 1 ke store ID pertama
    url1 = base_url + store_ids[0]
    print(f"🔄 Navigating Window 1 to business hours settings (Store ID: {store_ids[0]}): {url1}")
    driver.get(url1)
    time.sleep(2)
    
    print("🌐 Opening 2 additional windows (total 3 windows)...")
    windows = [driver.current_window_handle]
    for i in range(2):
        driver.switch_to.new_window('window')
        url = base_url + store_ids[i + 1]
        print(f"🔄 Navigating Window {i + 2} to business hours settings (Store ID: {store_ids[i + 1]}): {url}")
        driver.get(url)
        windows.append(driver.current_window_handle)
        time.sleep(2)
        
    print(f"✅ Total windows opened: {len(windows)}")
    
    # ── Inject Monkeypatch on document creation & current page for all windows ──
    print("💉 Injecting network interceptor into all windows...")
    for idx, handle in enumerate(windows, 1):
        driver.switch_to.window(handle)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": MONKEYPATCH_JS})
        try:
            driver.execute_script(MONKEYPATCH_JS)
            print(f"  ✅ Interceptor injected on Window {idx}")
        except Exception as e:
            print(f"  ⚠️ Injection warning on Window {idx}: {e}")
            
    # ── Step 3: Trigger Merchant Switch on Window 1 ──
    print(f"🔄 Triggering switch to: '{target_merchant}' on Window 1...")
    driver.switch_to.window(windows[0])
    switch_success = browser.auto_switch_merchant(driver, target_merchant)
    
    if switch_success:
        print(f"🎉 Successfully switched to merchant: '{target_merchant}'!")
    else:
        print(f"❌ Failed to switch to merchant: '{target_merchant}'.")
        
    # ── Step 4: Retrieve and print captured traffic logs from the switch process ──
    print("\n📊 Polling captured network logs from all active windows...")
    time.sleep(2)  # Give a moment for any final requests to resolve
    
    logs = []
    for idx, handle in enumerate(windows, 1):
        try:
            driver.switch_to.window(handle)
            w_logs = driver.execute_script("""
                try {
                    const logs = JSON.parse(sessionStorage.getItem('__captured_logs') || '[]');
                    sessionStorage.removeItem('__captured_logs');
                    return logs;
                } catch(e) {
                    return [];
                }
            """)
            if w_logs:
                for item in w_logs:
                    item['window_idx'] = idx
                logs.extend(w_logs)
        except Exception as e:
            print(f"⚠️ Failed to retrieve logs from Window {idx}: {e}")
            
    if logs:
        print(f"✅ Captured {len(logs)} network events during the switch:")
        print("-" * 80)
        for log_idx, log_item in enumerate(logs, 1):
            log_type = log_item.get("type", "api")
            url = log_item.get("url", "").split("?")[0]
            method = log_item.get("method", "GET")
            status = log_item.get("status", "")
            w_idx = log_item.get("window_idx", 1)
            
            status_str = f" [Status: {status}]" if status else ""
            print(f"[{log_idx}] Window {w_idx} | {log_type.upper()} | {method} | {url}{status_str}")
            
            # Print a snippet of request/response body if present
            req_body = log_item.get("request_body", "")
            res_body = log_item.get("response_body", "")
            if req_body.strip():
                print(f"    Request: {req_body[:100]}...")
            if res_body.strip():
                print(f"    Response: {res_body[:100]}...")
        print("-" * 80)
    else:
        print("ℹ️ No network logs captured during the switch process (no new Fetch/XHR/WebSocket messages detected).")

    print("\n🏁 Test completed. Press Ctrl+C in this terminal to close the browser.")
    try:
        while True:
            time.sleep(1)
            # Check if any browser windows are still open
            any_open = False
            for handle in windows:
                try:
                    driver.switch_to.window(handle)
                    _ = driver.current_url
                    any_open = True
                    break
                except:
                    pass
            if not any_open:
                break
    except KeyboardInterrupt:
        print("\n🛑 Closing browser...")
    finally:
        try:
            driver.quit()
        except:
            pass
        print("✅ Finished.")


if __name__ == "__main__":
    main()
