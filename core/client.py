"""
core/client.py
==============
Unified HTTP client for Shopee Seller API and Shopee Buyer API.

- Seller API  : requires tob_token + entity_id (from login)
- Buyer API   : public endpoints (no auth required)
"""

import requests
from core.logger import get_logger

log = get_logger("client")

SELLER_BASE = "https://foody.shopee.co.id"
BUYER_BASE  = "https://shopee.co.id"
IMG_BASE    = "https://down-id.img.susercontent.com/file"


def build_img_url(img_id: str) -> str:
    """Convert a Shopee image ID to a full CDN URL."""
    return f"{IMG_BASE}/{img_id}" if img_id else ""


class ShopeeClient:
    def __init__(self, tob_token: str, entity_id: str, extra_cookies: dict = None):
        self.tob_token     = tob_token
        self.extra_cookies = extra_cookies or {}
        # Resolve entity_id: prefer explicit value, fall back to shopee_foody_mid cookie
        self.entity_id = (
            entity_id
            or self.extra_cookies.get("shopee_foody_mid", "")
        )
        if self.entity_id:
            log.debug(f"ShopeeClient initialized | entity_id={self.entity_id}")
        else:
            log.warning("[API] ShopeeClient: entity_id is empty. API calls may fail.")
        self.session = requests.Session()
        self.user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"

    # ── Header builders ────────────────────────────────────────────────────────

    def _seller_headers(self, override_entity_id: str = None) -> dict:
        """Headers for the Shopee Seller/Partner API."""
        eid = override_entity_id or self.entity_id

        # Build cookie string: start from all saved cookies, then override auth
        cookies = self.extra_cookies.copy()
        cookies["shopee_tob_token"]     = self.tob_token
        cookies["shopee_tob_entity_id"] = eid
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

        return {
            "Host":           "foody.shopee.co.id",
            "Accept":         "application/json, text/plain, */*",
            "Content-Type":   "application/json",
            "User-Agent":     self.user_agent,
            "Cookie":         cookie_str,
            "X-Sf-Platform":  "2",
            "Operate-Source": "partnerapp",
            "Origin":         "https://partner.shopee.co.id",
            "Referer":        "https://partner.shopee.co.id/",
        }

    def _partner_headers(self) -> dict:
        """Headers for the NEW Partner API (api.partner.shopee.co.id).
        Used by ExportTransactionList and GetReportList endpoints.
        """
        import uuid
        return {
            "accept":              "application/json, text/plain, */*",
            "accept-encoding":     "gzip, deflate, br, zstd",
            "accept-language":     "en-US,en;q=0.9",
            "content-type":        "application/json",
            "origin":              "https://partner.shopee.co.id",
            "referer":             "https://partner.shopee.co.id/",
            "user-agent":          self.user_agent,
            "shopee-baggage":      "PFB=undefined",
            "x-merchant-from":     "12",
            "x-merchant-language":  "id",
            "x-merchant-login-from": "12",
            "x-merchant-requestid": str(uuid.uuid4()),
            "x-merchant-timezone":  "Asia/Jakarta",
            "x-merchant-tob-clientid": "undefined",
            "x-merchant-token":    self.tob_token,
        }

    def _buyer_headers(self) -> dict:
        """Headers for the public Shopee Food Buyer API (no auth)."""
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        }

    # ── Seller API methods ─────────────────────────────────────────────────────

    def get_all_stores(self) -> list[dict]:
        """
        POST /api/seller/stores
        Returns a flat list of all stores via pagination (max 50 per page).
        """
        url = f"{SELLER_BASE}/api/seller/stores"
        all_stores = []
        page_no = 1
        page_size = 50
        
        while True:
            payload = {"page_no": page_no, "page_size": page_size}
            try:
                resp = self.session.post(url, json=payload, headers=self._seller_headers(), timeout=15)
                data = resp.json()
                if data.get("code") == 0:
                    stores = data.get("data", {}).get("stores", [])
                    total  = data.get("data", {}).get("total", 0)
                    all_stores.extend(stores)
                    if len(all_stores) >= total or not stores:
                        break
                    page_no += 1
                else:
                    log.warning(f"get_all_stores failed at page {page_no}: code={data.get('code')} msg={data.get('msg')}")
                    break
            except Exception as e:
                log.error(f"get_all_stores error at page {page_no}: {e}")
                break
                
        return all_stores

    def get_store_detail(self, store_id: str) -> dict:
        """
        GET /api/seller/store
        Returns full detail for one store including:
        lat/long, rating, rater count, logo, banner, address, status.

        Note: The API uses the entity_id in the cookie to identify the store,
        so we override it per call.
        """
        url = f"{SELLER_BASE}/api/seller/store"
        try:
            resp = requests.get(
                url,
                headers=self._seller_headers(override_entity_id=store_id),
                timeout=15,
            )
            data = resp.json()
            if data.get("code") == 0:
                return data.get("data", {})
            log.warning(f"get_store_detail({store_id}) failed: code={data.get('code')}")
        except Exception as e:
            log.error(f"get_store_detail({store_id}) error: {e}")
        return {}

    def get_store_dishes(self, store_id: str) -> list[dict]:
        """
        GET /api/seller/store/dishes
        Returns list of catalogs, each containing a list of dishes.
        Price unit: divide by 100,000 to get IDR.
        """
        url = f"{SELLER_BASE}/api/seller/store/dishes"
        try:
            resp = requests.get(
                url,
                headers=self._seller_headers(override_entity_id=store_id),
                timeout=15,
            )
            data = resp.json()
            if data.get("code") == 0:
                return data.get("data", {}).get("catalogs", [])
            log.warning(f"get_store_dishes({store_id}) failed: code={data.get('code')}")
        except Exception as e:
            log.error(f"get_store_dishes({store_id}) error: {e}")
        return []

    def get_store_option_groups(self, store_id: str, dish_ids: list = None) -> list[dict]:
        """
        POST /api/seller/store/option-groups/search
        Returns list of modifier groups and their options.
        If dish_ids is provided, it filters modifiers specific to those dishes.
        """
        url = f"{SELLER_BASE}/api/seller/store/option-groups/search"
        payload = {"page_no": 1, "page_size": 100}
        
        if dish_ids:
            payload["filter"] = {"dish_ids": dish_ids}
            
        try:
            resp = self.session.post(
                url,
                json=payload,
                headers=self._seller_headers(override_entity_id=store_id),
                timeout=15,
            )
            data = resp.json()
            if data.get("code") == 0:
                return data.get("data", {}).get("option_groups", [])
            log.warning(f"get_store_option_groups({store_id}) failed: code={data.get('code')}")
        except Exception as e:
            log.error(f"get_store_option_groups({store_id}) error: {e}")
        return []

    def get_transaction_list(self, store_id: str, start_time: int, end_time: int, page_no: int = 1, page_size: int = 50) -> list[dict]:
        """
        POST /nb/mss/web-api/PartnerTransactionServer/GetTransactionList
        Fetches the list of generic transactions (orders) internally from the gRPC gateway.
        """
        url = "https://api.partner.shopee.co.id/nb/mss/web-api/PartnerTransactionServer/GetTransactionList"
        # Because this is a different gateway, we need different headers
        headers = {
            "content-type": "application/json",
            "x-merchant-token": self.tob_token,
            "x-merchant-from": str(self.entity_id),
            "x-merchant-language": "id",
            "x-merchant-login-from": "12",
            "x-merchant-requestid": "auto-gen-req-id", # usually doesn't aggressively validate UUID formatting 
            "x-merchant-timezone": "Asia/Jakarta",
            "x-merchant-storeid": str(store_id)
        }
        
        payload = {
            "pageNo": page_no,
            "pageSize": page_size,
            "filter": {
                "storeIdList": [int(store_id)],
                "startTime": start_time,
                "endTime": end_time,
                "serviceList": [2]
            },
            "sorter": {"field": "createTime", "order": "descend"}
        }

        try:
            resp = self.session.post(url, json=payload, headers=headers)
            data = resp.json()
            if data.get("errorCode") == 0:
                result = data.get("data") or {}
                return result.get("list", []), int(result.get("total", 0))
            elif data.get("errorMsg"):
                log.warning(f"get_transaction_list error: {data.get('errorMsg')}")
        except Exception as e:
            log.error(f"get_transaction_list({store_id}) exception: {e}")
            
        return [], 0

    def get_order_detail(self, order_id: str, store_id: str) -> dict:
        """
        GET /api/seller/mis/orders/{order_id}
        Requires header: shopee_tob_entity_id set to the specific store_id for permission.
        """
        url = f"{SELLER_BASE}/api/seller/mis/orders/{order_id}"
        # Override entity_id dynamically via the built-in param
        headers = self._seller_headers(override_entity_id=str(store_id))
        
        try:
            resp = self.session.get(url, headers=headers)
            return resp.json().get("data", {})
        except Exception as e:
            log.error(f"get_order_detail({order_id}) error: {e}")
        return {}

    def export_transaction_report(self, store_ids: list = None, merchant_ids: list = None, start_time: int = 0, end_time: int = 0) -> bool:
        """
        POST /nb/mss/web-api/PartnerTransactionServer/ExportTransactionList
        Triggers an Excel export for the given list of stores OR merchants and time range.
        """
        url = "https://api.partner.shopee.co.id/nb/mss/web-api/PartnerTransactionServer/ExportTransactionList"
        headers = self._partner_headers()
        
        filter_payload = {
            "startTime": start_time,
            "endTime": end_time,
            "serviceList": [2]
        }
        
        if store_ids:
            filter_payload["storeIdList"] = [int(sid) for sid in store_ids]
            headers["x-merchant-storeid"] = str(store_ids[0])
        
        if merchant_ids:
            filter_payload["merchantIdList"] = [int(mid) for mid in merchant_ids]
            # When exporting by merchant, we might need x-merchant-merchantid header
            headers["x-merchant-merchantid"] = str(merchant_ids[0])

        payload = {
            "pageNo": 1,
            "pageSize": 10,
            "filter": filter_payload,
            "sorter": {"field": "createTime", "order": "descend"}
        }

        try:
            resp = self.session.post(url, json=payload, headers=headers)
            data = resp.json()
            if data.get("errorCode") == 0:
                log.info(f"✅ [API] Export triggered successfully for {'stores' if store_ids else 'merchants'}")
                return True
            log.warning(f"[API] export_transaction_report failed: {data.get('errorMsg')}")
        except Exception as e:
            log.error(f"export_transaction_report exception: {e}")
            return None
            
        return False

    def get_report_list(self) -> list[dict]:
        """
        POST /nb/mss/web-api/PartnerReportServer/GetReportList
        Returns the list of generated reports with normalized field names.
        """
        url = "https://api.partner.shopee.co.id/nb/mss/web-api/PartnerReportServer/GetReportList"
        headers = self._partner_headers()
        
        payload = {
            "filter": {
                "reportTypeList": [2],
                "serviceList": [2]
            },
            "pageNo": 1,
            "pageSize": 20
        }

        try:
            resp = self.session.post(url, json=payload, headers=headers)
            data = resp.json()
            if data.get("errorCode") == 0:
                reports = data.get("data", {}).get("reportInfoList", [])
                normalized = []
                for r in reports:
                    # Extract timestamps from filterData if available
                    st, et = None, None
                    if r.get("filterData"):
                        try:
                            f_data = json.loads(r["filterData"])
                            st = f_data.get("startTime")
                            et = f_data.get("endTime")
                        except: pass
                    
                    normalized.append({
                        "id": r.get("reportId"),
                        "name": r.get("reportName"),
                        "status": r.get("reportStatus"), # 2 usually means success/ready
                        "download_url": r.get("downLoadUrl"),
                        "create_time": r.get("createTime"),
                        "start_time": st or r.get("startTime"),
                        "end_time": et or r.get("endTime")
                    })
                return normalized
            log.warning(f"get_report_list failed: {data.get('errorMsg')}")
        except Exception as e:
            log.error(f"get_report_list exception: {e}")
            return None
            
        return []

    def _wallet_headers(self) -> dict:
        """Headers for Wallet API (Merchant-level context, shopee_tob_entity_id cookie must be empty)."""
        cookies = self.extra_cookies.copy()
        cookies["shopee_tob_token"]     = self.tob_token
        cookies["shopee_tob_entity_id"] = ""
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

        return {
            "Host":           "foody.shopee.co.id",
            "Accept":         "application/json, text/plain, */*",
            "Content-Type":   "application/json",
            "User-Agent":     self.user_agent,
            "Cookie":         cookie_str,
            "X-Sf-Platform":  "2",
            "Operate-Source": "partnerapp",
            "Origin":         "https://partner.shopee.co.id",
            "Referer":        "https://partner.shopee.co.id/",
        }

    def submit_wallet_export(self, start_time: int, end_time: int, wallet_id: str = None) -> str:
        """
        POST /api/seller/v1/wallet/export-task/submit
        Triggers an Excel export of wallet transactions for the given time range.
        Returns the task_id if successful, or None/False.
        """
        url = "https://foody.shopee.co.id/api/seller/v1/wallet/export-task/submit"
        
        # Static mapping fallback for VB portals if wallet_id is not passed
        wallet_map = {
            "11511947": "13629594", # portal_f (SuperFood)
            "14367488": "16612848", # portal_w (WonderFood)
            "14384953": "16634272", # portal_l (LOKARASA)
            "15892383": "18362777", # portal_d (Gurame Bakar, Do Eat)
        }
        
        target_wallet_id = wallet_id or wallet_map.get(str(self.entity_id))
        search_wallet_ids = [target_wallet_id] if target_wallet_id else []
        
        payload = {
            "export_type": 56,
            "filter": {
                "search_start_time": str(start_time),
                "search_end_time": str(end_time),
                "search_wallet_ids": search_wallet_ids
            }
        }
        try:
            resp = self.session.post(url, json=payload, headers=self._wallet_headers(), timeout=15)
            data = resp.json()
            if data.get("code") == 0:
                task_id = data.get("data", {}).get("task_id")
                log.info(f"✅ [API] Wallet export triggered successfully: task_id={task_id}")
                return str(task_id)
            log.warning(f"[API] submit_wallet_export failed: code={data.get('code')} msg={data.get('msg')}")
        except Exception as e:
            log.error(f"submit_wallet_export exception: {e}")
            return None
        return False

    def get_wallet_report_list(self) -> list[dict]:
        """
        GET /api/seller/v1/wallet/export-task/list
        Returns the list of generated wallet reports.
        """
        url = "https://foody.shopee.co.id/api/seller/v1/wallet/export-task/list?export_type=56&page_num=1&page_size=20"
        try:
            resp = self.session.get(url, headers=self._wallet_headers(), timeout=15)
            data = resp.json()
            if data.get("code") == 0:
                tasks = data.get("data", {}).get("task_list", [])
                normalized = []
                for t in tasks:
                    normalized.append({
                        "id": str(t.get("task_id")),
                        "name": t.get("task_name"),
                        "status": t.get("task_status"), # 3 means ready/success
                        "download_url": t.get("file_url"),
                        "create_time": 0 # Not provided directly in task_list items
                    })
                return normalized
            log.warning(f"get_wallet_report_list failed: code={data.get('code')} msg={data.get('msg')}")
        except Exception as e:
            log.error(f"get_wallet_report_list exception: {e}")
            return None
        return []

    def search_wallet_transactions(
        self,
        start_time: int,
        end_time: int,
        wallet_id: str = None,
        page_size: int = 50,
    ) -> list[dict]:
        """
        POST /api/seller/v1/wallet/transaction/search
        Fetches wallet transactions directly (paginated). Returns a flat list of all
        transaction records for the given time range.
        Documented in src/VB/shopee/API/search-transaction-*.

        Note: Shopee enforces a max page_size of 50. Values above 50 cause ERROR_PARAMS_INVALID.
        """
        url = f"{SELLER_BASE}/api/seller/v1/wallet/transaction/search"

        # Enforce Shopee's page size cap
        page_size = min(page_size, 50)

        # Resolve wallet_id from static VB portal map if not provided
        wallet_map = {
            "11511947": "13629594",  # portal_f (SuperFood)
            "14367488": "16612848",  # portal_w (WonderFood)
            "14384953": "16634272",  # portal_l (LOKARASA)
            "15892383": "18362777",  # portal_d (Gurame Bakar, Do Eat)
        }
        mw = wallet_id or wallet_map.get(str(self.entity_id), "")

        all_records: list[dict] = []
        page_num = 1

        while True:
            payload: dict = {
                "filter": {
                    "search_start_time": str(start_time),
                    "search_end_time": str(end_time),
                },
                "page_num": page_num,
                "page_size": page_size,
            }
            # Only include mw when we have a valid wallet ID — empty string causes ERROR_PARAMS_INVALID
            if mw:
                payload["mw"] = mw

            try:
                resp = self.session.post(
                    url,
                    json=payload,
                    headers=self._wallet_headers(),
                    timeout=20,
                )
                data = resp.json()
                if data.get("code") != 0:
                    log.warning(
                        f"search_wallet_transactions page {page_num} failed: "
                        f"code={data.get('code')} msg={data.get('msg')}"
                    )
                    break

                records = data.get("data", {}).get("transaction_logs", [])
                total = data.get("data", {}).get("total", 0)
                all_records.extend(records)

                log.info(
                    f"  📄 [WALLET SEARCH] Page {page_num}: "
                    f"{len(records)} records fetched, total={total}"
                )

                if len(all_records) >= total or len(records) == 0:
                    break
                page_num += 1
            except Exception as e:
                log.error(f"search_wallet_transactions exception (page {page_num}): {e}")
                break

        return all_records

    # ── Buyer API methods (public, no auth) ────────────────────────────────────

    def get_public_store_detail(self, store_id: str) -> dict:
        """
        Shopee Food Buyer API — public store detail.
        May contain additional public info like category tags and public rating.
        """
        url    = f"{BUYER_BASE}/api/v4/shopee_food/get_store_detail"
        params = {"store_id": store_id}
        try:
            resp = requests.get(url, params=params, headers=self._buyer_headers(), timeout=10)
            data = resp.json()
            if data.get("error") == 0:
                return data.get("data", {})
        except Exception as e:
            log.debug(f"get_public_store_detail({store_id}) error: {e}")
        return {}

    def get_public_reviews(self, store_id: str, limit: int = 50) -> list[dict]:
        """
        Shopee Food Buyer API — customer reviews.
        Returns list of review dicts.
        """
        url    = f"{BUYER_BASE}/api/v4/shopee_food/get_review_list"
        params = {"store_id": store_id, "offset": 0, "limit": limit}
        try:
            resp = requests.get(url, params=params, headers=self._buyer_headers(), timeout=10)
            data = resp.json()
            if data.get("error") == 0:
                return data.get("data", {}).get("reviews", [])
        except Exception as e:
            log.debug(f"get_public_reviews({store_id}) error: {e}")
        return []
