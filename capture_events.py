#!/usr/bin/env python3
"""
capture_events.py
=================
Sniffs, intercepts, and logs network events (API fetch/XHR requests and WebSockets)
on the Shopee Partner Dashboard to investigate outlet temporary closures.

Usage:
    /home/akbarhann/project/FoodMaster/baseline/src/.venv/bin/python capture_events.py <account_name>
Example:
    /home/akbarhann/project/FoodMaster/baseline/src/.venv/bin/python capture_events.py auto7307
"""

import os
import sys
import time
import json
import csv
import re
from pathlib import Path
from datetime import datetime

# ── Path Setup ─────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# Ensure data directory exists
DATA_DIR = SCRIPT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


def list_available_accounts():
    """Lists all account profiles found in the data directory."""
    sessions = list(DATA_DIR.glob("session_*.json"))
    accounts = []
    for s in sessions:
        # Extract account name from session_NAME.json
        name = s.stem.replace("session_", "")
        accounts.append(name)
    return sorted(accounts)


def select_account():
    """Prompts the user to select an account or parses command line args."""
    accounts = list_available_accounts()
    
    if len(sys.argv) > 1:
        arg_account = sys.argv[1]
        if arg_account in accounts:
            return arg_account
        else:
            print(f"⚠️ Account '{arg_account}' not found in saved sessions.")
            print(f"Available sessions: {', '.join(accounts) if accounts else 'None'}")
            print("Proceeding with custom login using credentials.json...")
            return arg_account

    if not accounts:
        print("❌ No saved account sessions found in data/.")
        print("Please check credentials.json or run a session warmer script first.")
        # If credentials.json exists, we can try using the shopee_username from it
        cred_file = SCRIPT_DIR / "credentials.json"
        if cred_file.exists():
            try:
                creds = json.loads(cred_file.read_text())
                username = creds.get("shopee_username")
                if username:
                    print(f"👉 Found username in credentials.json: '{username}'. Using it.")
                    return username
            except Exception as e:
                print(f"Error reading credentials.json: {e}")
        
        # Fallback prompt
        username = input("Enter Shopee username/account name to use: ").strip()
        if not username:
            print("❌ Username cannot be empty.")
            sys.exit(1)
        return username

    print("\n--- Shopee Partner Portal Accounts ---")
    for idx, acc in enumerate(accounts, 1):
        print(f"  [{idx}] {acc}")
    print("--------------------------------------")
    
    while True:
        try:
            choice = input(f"Select account (1-{len(accounts)}) [default: 1]: ").strip()
            if not choice:
                return accounts[0]
            idx = int(choice)
            if 1 <= idx <= len(accounts):
                return accounts[idx - 1]
            else:
                print(f"Please enter a number between 1 and {len(accounts)}.")
        except ValueError:
            print("Invalid input. Please enter a number.")


# ── JavaScript Interceptor Code ────────────────────────────────────────────────
MONKEYPATCH_JS = """
(function() {
    if (window.__event_capture_injected) return;
    window.__event_capture_injected = true;

    // Helper to get current stored logs
    function getStoredLogs() {
        try {
            return JSON.parse(sessionStorage.getItem('__captured_logs') || '[]');
        } catch (e) {
            return [];
        }
    }

    // Helper to save logs safely
    function saveStoredLogs(logs) {
        try {
            // Keep storage clean if it gets too large
            if (logs.length > 500) {
                logs = logs.slice(-300); // keep last 300
            }
            sessionStorage.setItem('__captured_logs', JSON.stringify(logs));
        } catch (e) {
            console.error('[Event Capture] Storage error:', e);
        }
    }

    // Helper to log a network event
    function logEvent(type, url, method, reqBody, status, resBody) {
        // Filter out static assets to prevent log pollution
        const lowerUrl = String(url || '').toLowerCase();
        const isAsset = lowerUrl.endsWith('.js') || lowerUrl.endsWith('.css') || 
                        lowerUrl.endsWith('.png') || lowerUrl.endsWith('.jpg') || 
                        lowerUrl.endsWith('.jpeg') || lowerUrl.endsWith('.gif') || 
                        lowerUrl.endsWith('.svg') || lowerUrl.endsWith('.woff') || 
                        lowerUrl.endsWith('.woff2') || lowerUrl.includes('/static/');
                        
        if (isAsset) return;

        // Truncate overly long bodies in memory to avoid storage quota issues
        const maxLen = 150000; // 150KB limit per request body
        let truncatedResBody = String(resBody || '');
        if (truncatedResBody.length > maxLen) {
            truncatedResBody = truncatedResBody.substring(0, maxLen) + '\\n\\n...[TRUNCATED IN JS INTERCEPTOR]...';
        }

        let truncatedReqBody = String(reqBody || '');
        if (truncatedReqBody.length > maxLen) {
            truncatedReqBody = truncatedReqBody.substring(0, maxLen) + '\\n\\n...[TRUNCATED IN JS INTERCEPTOR]...';
        }

        const logs = getStoredLogs();
        logs.push({
            timestamp: new Date().toISOString(),
            type: type,
            url: url,
            method: method,
            request_body: truncatedReqBody,
            status: status,
            response_body: truncatedResBody,
            page_url: window.location.href
        });
        saveStoredLogs(logs);
    }

    // ── 1. Intercept Fetch ──────────────────────────────────────────────────
    const originalFetch = window.fetch;
    window.fetch = async function(...args) {
        let url = '';
        if (args[0]) {
            if (typeof args[0] === 'string') url = args[0];
            else if (args[0] instanceof URL) url = args[0].href;
            else if (args[0].url) url = args[0].url;
        }
        const options = args[1] || {};
        const method = options.method || 'GET';
        let reqBody = '';
        
        if (options.body) {
            try {
                if (typeof options.body === 'string') reqBody = options.body;
                else if (options.body instanceof Blob) reqBody = '[Blob]';
                else if (options.body instanceof FormData) {
                    const obj = {};
                    for (let [k, v] of options.body.entries()) {
                        obj[k] = (typeof v === 'string' || v instanceof String) ? v : `[File: ${v.name}]`;
                    }
                    reqBody = JSON.stringify(obj);
                } else {
                    reqBody = JSON.stringify(options.body);
                }
            } catch (e) {
                reqBody = `[Error parsing body: ${e.message}]`;
            }
        }

        try {
            const response = await originalFetch.apply(this, args);
            const clonedResponse = response.clone();
            clonedResponse.text().then(text => {
                logEvent('fetch', url, method, reqBody, response.status, text);
            }).catch(err => {
                logEvent('fetch', url, method, reqBody, response.status, `[Error reading response: ${err.message}]`);
            });
            return response;
        } catch (error) {
            logEvent('fetch_error', url, method, reqBody, 0, error.message);
            throw error;
        }
    };

    // ── 2. Intercept XMLHttpRequest ──────────────────────────────────────────
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSend = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.open = function(method, url, ...args) {
        this._url = (url instanceof URL) ? url.href : url;
        this._method = method;
        return originalOpen.apply(this, [method, url, ...args]);
    };

    XMLHttpRequest.prototype.send = function(body) {
        let reqBody = '';
        if (body) {
            try {
                if (typeof body === 'string') reqBody = body;
                else reqBody = JSON.stringify(body);
            } catch (e) {
                reqBody = '[XHR Body]';
            }
        }
        
        this.addEventListener('load', function() {
            let resBody = '';
            try {
                resBody = this.responseText;
            } catch (e) {
                resBody = '[Error reading responseText]';
            }
            logEvent('xhr', this._url, this._method, reqBody, this.status, resBody);
        });

        this.addEventListener('error', function() {
            logEvent('xhr_error', this._url, this._method, reqBody, this.status, 'Network Error');
        });

        return originalSend.apply(this, [body]);
    };

    // ── 3. Intercept WebSockets ──────────────────────────────────────────────
    const originalWebSocket = window.WebSocket;
    window.WebSocket = function(url, protocols) {
        const ws = new originalWebSocket(url, protocols);
        logEvent('websocket_open', url, 'WS_CONNECT', '', 101, '');

        const originalSend = ws.send;
        ws.send = function(data) {
            logEvent('websocket_send', url, 'WS_SEND', data, 101, '');
            return originalSend.apply(this, [data]);
        };

        ws.addEventListener('message', function(event) {
            logEvent('websocket_receive', url, 'WS_RECV', '', 101, event.data);
        });

        ws.addEventListener('close', function(event) {
            logEvent('websocket_close', url, 'WS_CLOSE', `Code: ${event.code}, Reason: ${event.reason}`, 101, '');
        });

        return ws;
    };
    for (let key in originalWebSocket) {
        if (originalWebSocket.hasOwnProperty(key)) {
            window.WebSocket[key] = originalWebSocket[key];
        }
    }
    window.WebSocket.prototype = originalWebSocket.prototype;
    
    console.log('✅ [Event Capture] Interceptor monkeypatch successfully loaded.');
})();
"""


def main():
    account_name = select_account()
    print(f"\n🎯 Target Account: {account_name}")
    
    # ── Path Setup ─────────────────────────────────────────────────────────────
    # Output logs in captured_logs/{account_name}/
    out_dir = SCRIPT_DIR / "captured_logs" / account_name
    payloads_dir = out_dir / "payloads"
    payloads_dir.mkdir(parents=True, exist_ok=True)
    
    # CSV file name with timestamp
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_file = out_dir / f"traffic_log_{timestamp_str}.csv"
    
    print(f"📁 Saving CSV logs to: {csv_file}")
    print(f"📁 Saving detailed payloads to: {payloads_dir}/")
    
    # ── Initialize CSV ─────────────────────────────────────────────────────────
    csv_headers = [
        "Timestamp",
        "Type",
        "Method",
        "URL",
        "Status",
        "Request Snippet",
        "Response Snippet",
        "Payload File",
        "Page URL",
        "Window"
    ]
    
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(csv_headers)

    # ── Import & Configure Browser ─────────────────────────────────────────────
    from core import browser
    
    session_file = DATA_DIR / f"session_{account_name}.json"
    browser.set_session_file(session_file)
    
    print("🌐 Loading credentials from credentials.json...")
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

    print("🌐 Launching Chrome Browser...")
    
    # Run in headless mode
    session_data = browser.get_session(
        username=username or None,
        password=password or None,
        phone=phone or None,
        headless=True,
        close_browser=False,
        interactive=True
    )
    
    if not session_data or "driver" not in session_data:
        print("❌ Failed to initiate driver session.")
        sys.exit(1)
        
    driver = session_data["driver"]
    print("✅ Chrome started!")
    
    dashboard_url = "https://partner.shopee.co.id/food/dashboard"
    store_ids = ["21708903", "21830864", "21708892"]
    base_url = "https://partner.shopee.co.id/settings/shopee-food/business-hours-settings/business-hours?storeId="
    
    # Navigasi ke Shopee Partner Dashboard jika belum di sana untuk verifikasi login
    if "dashboard" not in driver.current_url.lower() and "merchant-selector" not in driver.current_url.lower():
        print(f"🔄 Navigating Window 1 to Shopee Partner Dashboard to verify login...")
        driver.get(dashboard_url)
        time.sleep(3)

    # Navigasi Window 1 ke store ID pertama
    url1 = base_url + store_ids[0]
    print(f"🔄 Navigating Window 1 to business hours settings (Store ID: {store_ids[0]}): {url1}")
    driver.get(url1)
    time.sleep(2)

    # Open 2 additional windows dan arahkan masing-masing ke store berikutnya
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

    print()
    print("=" * 70)
    print("🟢 EVENT CAPTURE ACTIVE. Monitoring Network Traffic in 3 Windows...")
    print("    Feel free to interact with the dashboards.")
    print("    Outlet status changes and API requests will be logged below in real-time.")
    print("    Press Ctrl+C in this terminal to stop and close the browser.")
    print("=" * 70)
    print()

    # Track last page URLs to log navigation transitions
    last_page_urls = {}
    for idx, handle in enumerate(windows, 1):
        try:
            driver.switch_to.window(handle)
            last_page_urls[handle] = driver.current_url
        except:
            pass
            
    # Clean file name generator
    payload_counter = 0

    try:
        while True:
            # Check active windows (and prune closed ones)
            active_windows = []
            for handle in list(windows):
                try:
                    driver.switch_to.window(handle)
                    _ = driver.current_url
                    active_windows.append(handle)
                except Exception:
                    pass
            
            if not active_windows:
                print("\n🔴 All browser windows closed by user.")
                break
                
            windows = active_windows
                
            # Log URL changes per window
            for idx, handle in enumerate(windows, 1):
                try:
                    driver.switch_to.window(handle)
                    current_url = driver.current_url
                    last_url = last_page_urls.get(handle)
                    if current_url != last_url:
                        timestamp = datetime.now().isoformat()
                        print(f"🔗 [Window {idx} URL CHANGE] -> {current_url}")
                        with open(csv_file, "a", newline="", encoding="utf-8") as f:
                            writer = csv.writer(f)
                            writer.writerow([
                                timestamp,
                                "url_change",
                                f"NAVIGATION_WINDOW_{idx}",
                                current_url,
                                "",
                                "",
                                "",
                                "",
                                current_url
                            ])
                        last_page_urls[handle] = current_url
                        
                        # Re-inject monkeypatch after navigation
                        try:
                            driver.execute_script(MONKEYPATCH_JS)
                        except:
                            pass
                except Exception:
                    pass

            # Poll for new logs from all active windows
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
                        for log_item in w_logs:
                            log_item["window_idx"] = idx
                        logs.extend(w_logs)
                except Exception:
                    pass
                
            if logs:
                for log_item in logs:
                    timestamp = log_item.get("timestamp")
                    log_type = log_item.get("type", "api")
                    url = log_item.get("url", "")
                    method = log_item.get("method", "GET")
                    req_body = log_item.get("request_body", "")
                    status = log_item.get("status", "")
                    res_body = log_item.get("response_body", "")
                    page_url = log_item.get("page_url", "")
                    
                    # Formatting snippets for stdout and CSV summary
                    req_snippet = (req_body[:150] + "...") if len(req_body) > 150 else req_body
                    res_snippet = (res_body[:150] + "...") if len(res_body) > 150 else res_body
                    
                    # Highlight important events (e.g. status changes, errors, outlet close events)
                    # Let's inspect url and body for patterns related to store closing/status
                    is_suspicious = False
                    
                    # Clean URL for display
                    clean_url = url.split("?")[0]
                    
                    # Explicit high-priority highlighting for store status action URLs requested by user
                    is_store_status_action = False
                    if "/opening-status/action/" in clean_url or "/api/seller/store" in clean_url:
                        is_store_status_action = True
                        is_suspicious = True
                        
                    # Print log item in stdout
                    arrow = "➡️" if "send" in log_type or method == "POST" else "⬅️"
                    status_str = f" [Status: {status}]" if status else ""
                    tag = f"[{log_type.upper()}]"
                    
                    if is_store_status_action:
                        print(f"🚨🚨 [STORE STATUS ACTION] {tag} {method} {arrow} {clean_url}{status_str}")
                        if req_body.strip():
                            print(f"   Payload: {req_body}")
                        if res_body.strip():
                            print(f"   Response: {res_body}")
                    elif is_suspicious:
                        print(f"🔥 {tag} {method} {arrow} {clean_url}{status_str}")
                        if req_snippet.strip():
                            print(f"   Payload: {req_snippet}")
                        if res_snippet.strip() and ("receive" in log_type or method != "POST"):
                            print(f"   Response: {res_snippet}")
                    else:
                        print(f"✨ {tag} {method} {arrow} {clean_url}{status_str}")
                        
                    # Save full payload to JSON if body exists
                    payload_file_rel = ""
                    if req_body.strip() or res_body.strip():
                        payload_counter += 1
                        payload_filename = f"payload_{timestamp_str}_{payload_counter:04d}.json"
                        payload_filepath = payloads_dir / payload_filename
                        
                        # Attempt to parse bodies as JSON for formatted saving
                        formatted_req = req_body
                        formatted_res = res_body
                        try:
                            formatted_req = json.loads(req_body)
                        except:
                            pass
                        try:
                            formatted_res = json.loads(res_body)
                        except:
                            pass
                            
                        payload_data = {
                            "timestamp": timestamp,
                            "type": log_type,
                            "url": url,
                            "method": method,
                            "status": status,
                            "page_url": page_url,
                            "window": f"Window {log_item.get('window_idx', 1)}",
                            "request_body": formatted_req,
                            "response_body": formatted_res
                        }
                        
                        with open(payload_filepath, "w", encoding="utf-8") as pf:
                            json.dump(payload_data, pf, indent=2, ensure_ascii=False)
                            
                        # Save relative path for CSV
                        payload_file_rel = f"payloads/{payload_filename}"

                    # Write to CSV
                    with open(csv_file, "a", newline="", encoding="utf-8") as f:
                        writer = csv.writer(f)
                        writer.writerow([
                            timestamp,
                            log_type,
                            method,
                            url,
                            status,
                            req_snippet,
                            res_snippet,
                            payload_file_rel,
                            page_url,
                            f"Window {log_item.get('window_idx', 1)}"
                        ])
            
            time.sleep(0.5)
            
    except KeyboardInterrupt:
        print("\n🛑 Stop requested by user.")
    finally:
        print("\n🧹 Cleaning up and closing browser...")
        try:
            driver.quit()
        except:
            pass
        print("✅ Finished. Check the CSV file and payloads directory for details!")


if __name__ == "__main__":
    main()
