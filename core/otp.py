import requests
import csv
import io
import time
from datetime import datetime, timedelta
from core.logger import get_logger

log = get_logger("otp")

SHEET_URL_BASE = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYSUnKOqk29LCktTxdb0wPLbWMbRaWRP3eC_UA4AwYod1FW6zDMhtLMC5ghIvot2B8upCDfBsn-TCP/pub?gid=213442295&single=true&output=csv"

def get_latest_otp(timeout_mins=10):
    """
    Fetches the latest OTP from the Google Sheet CSV.
    Only returns the OTP if it was received within the last `timeout_mins` minutes.
    """
    for attempt in range(3):
        try:
            log.info(f"📡 Fetching latest OTP from Google Sheet (Attempt {attempt+1}/3)...")
            # Add aggressive cache buster
            import random
            url = f"{SHEET_URL_BASE}&t={int(time.time())}&rand={random.randint(1000, 9999)}"
            headers = {
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
            response = requests.get(url, timeout=30, headers=headers)
            if response.status_code == 200:
                content = response.content.decode('utf-8')
                # Count lines to see if we are getting new data
                row_count = len(content.strip().split('\n')) - 1
                log.info(f"  📥 Received CSV with {row_count} rows.")
                break
            else:
                log.error(f"❌ Failed to fetch CSV: {response.status_code}")
                if attempt == 2: return None
        except requests.exceptions.ReadTimeout:
            log.warning(f"⏳ Read timeout on attempt {attempt+1}. Retrying...")
            if attempt == 2: return None
        except Exception as e:
            log.error(f"❌ Error fetching OTP: {e}")
            if attempt == 2: return None
        time.sleep(2)
    else:
        return None
    
    try:
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
        
        if not rows:
            log.warning("⚠️ No data found in OTP sheet.")
            return None
            
        # Log the latest timestamp for debugging
        latest_ts = rows[-1].get('Timestamp', 'Unknown')
        log.info(f"  📅 Latest row in CSV: {latest_ts}")

        # Scan rows in reverse to find the newest valid OTP
        now = datetime.now()
        for row in reversed(rows):
            timestamp_str = row.get('Timestamp', '').strip()
            otp_code = row.get('OTP', '').strip()
            
            if not timestamp_str or not otp_code:
                continue
                
            try:
                # Try multiple formats
                dt_otp = None
                formats = ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%d/%m/%Y %H:%M:%S']
                for fmt in formats:
                    try:
                        dt_otp = datetime.strptime(timestamp_str, fmt)
                        break
                    except ValueError: continue
                
                if not dt_otp: continue
                
                diff = now - dt_otp
                diff_seconds = diff.total_seconds()
                
                # Check if recent (within timeout_mins)
                # We also allow for slight clock drift (up to 2 minutes in the future)
                if -120 < diff_seconds < (timeout_mins * 60):
                    log.info(f"✅ Found valid OTP: {otp_code} (received {int(diff_seconds)}s ago)")
                    return otp_code
            except: continue
            
        # If we reach here, no recent OTP was found in the entire sheet
        if rows:
            last_row = rows[-1]
            t_str = last_row.get('Timestamp', 'Unknown')
            otp_val = last_row.get('OTP', '???')
            
            # Log exact age for debugging
            try:
                dt_last = None
                for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%d/%m/%Y %H:%M:%S']:
                    try:
                        dt_last = datetime.strptime(t_str, fmt)
                        break
                    except: continue
                if dt_last:
                    age_sec = (datetime.now() - dt_last).total_seconds()
                    log.warning(f"⏳ Stale OTP found: {otp_val} from {t_str} ({int(age_sec)}s ago). Waiting for newer...")
                else:
                    log.warning(f"⏳ Latest OTP in sheet is from {t_str}. Waiting for a newer one...")
            except:
                log.warning(f"⏳ Latest OTP in sheet is from {t_str}. Waiting for a newer one...")
            
        return None
            
    except Exception as e:
        log.error(f"❌ Error fetching OTP: {e}")
        return None

def wait_for_otp(max_wait_seconds=120, interval=5):
    """
    Polls the sheet until a recent OTP is found or timeout occurs.
    """
    start_time = time.time()
    log.info(f"⏳ Polling for new OTP (max {max_wait_seconds}s)...")
    
    while time.time() - start_time < max_wait_seconds:
        otp = get_latest_otp()
        if otp:
            return otp
        time.sleep(interval)
        
    log.error("❌ Timeout waiting for OTP.")
    return None
